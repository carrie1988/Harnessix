from __future__ import annotations

import asyncio
import stat

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.smoke import runner
from harnessix.smoke.contracts import SmokeConfig, SmokeReport
from tests.smoke.helpers import CANARY, WireFactory, config


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("HARNESSIX_SMOKE_TEST_KEY", CANARY)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)


@pytest.fixture(params=["openai_chat", "anthropic"])
def provider(request):
    return request.param


@pytest.mark.parametrize("scenario", ["text", "tool", "approval"])
async def test_real_sdk_scenarios_private_store_reopen_replay(provider, scenario, monkeypatch):
    factory = WireFactory()
    paths = []
    reopen_states = []
    original = runner.SQLiteSessionStore

    class InspectStore(original):
        async def initialize(self):
            await super().initialize()
            assert stat.S_IMODE(self.path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(self.path.stat().st_mode) == 0o600
            paths.append(self.path)
            for thread_id in await self.thread_ids():
                snapshot = await self.get_thread(thread_id)
                assert CANARY not in snapshot.model_dump_json()
                assert CANARY not in "".join(
                    e.model_dump_json() for e in await self.events(thread_id)
                )
                reopen_states.append(snapshot.turns[0].status)

    monkeypatch.setattr(runner, "SQLiteSessionStore", InspectStore)
    cfg = config(provider, scenario=scenario)
    report = await runner.run_smoke(cfg, allow_network=True, provider_factory=factory)
    assert report.reason == "passed", report
    assert report.execution == "injected"
    assert report.turn_status == TurnStatus.COMPLETED
    assert report.attempts_started == len(factory.requests) == (1 if scenario == "text" else 2)
    assert report.known_input_tokens == 10 * report.attempts_started
    assert report.known_output_tokens == 2 * report.attempts_started
    assert report.tool_calls == (0 if scenario == "text" else 1)
    assert report.approval_restart_verified == (scenario == "approval")
    assert report.replay_verified and report.usage_complete
    assert len(paths) == 2 and paths[0] == paths[1] and not paths[0].parent.exists()
    assert reopen_states == ["waiting_approval" if scenario == "approval" else "completed"]
    assert factory.closed and all(wire.closed for wire in factory.wires)
    assert factory.sdk._client.is_closed()
    assert SmokeReport.model_validate_json(report.model_dump_json()) == report
    for private in [
        CANARY,
        cfg.base_url,
        cfg.model,
        cfg.api_key_env,
        "chat-test",
        "msg-test",
        str(paths[0]),
    ]:
        assert private not in report.model_dump_json()
    for body in factory.requests:
        assert body["stream"] is True
        assert body.get("max_tokens", body.get("max_completion_tokens")) == 128
        if scenario == "text":
            assert "tools" not in body
        elif provider == "openai_chat":
            assert body["parallel_tool_calls"] is False
        else:
            assert body["tool_choice"]["disable_parallel_tool_use"] is True


@pytest.mark.parametrize("allow", [False, None, 0, 1, "true"])
async def test_explicit_gate_precedes_factory_and_credentials(allow):
    def forbidden(_):
        pytest.fail("不应创建 Provider")

    report = await runner.run_smoke(config(), allow_network=allow, provider_factory=forbidden)
    assert report.reason == "network_not_enabled" and report.attempts_started == 0


async def test_unvalidated_model_copy_cannot_bypass_limits():
    def forbidden(_):
        pytest.fail("不应创建 Provider")

    report = await runner.run_smoke(
        config().model_copy(update={"max_output_tokens": 10000}),
        allow_network=True,
        provider_factory=forbidden,
    )
    assert report.reason == "configuration_invalid"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limit"),
        (503, "provider_internal"),
    ],
)
async def test_http_failures_no_retry_and_no_body_echo(provider, status, code):
    factory = WireFactory(fault=status)
    report = await runner.run_smoke(config(provider), allow_network=True, provider_factory=factory)
    assert report.reason == "runtime_failed"
    assert report.provider_failure.code == code
    assert report.attempts_started == len(factory.requests) == 1
    assert not report.usage_complete
    assert report.replay_verified and factory.closed and factory.wires[0].closed
    assert CANARY not in report.model_dump_json()


@pytest.mark.parametrize(
    "fault", ["wrong_marker", "no_tool", "bad_arguments", "repeat_tool", "no_usage", "truncated"]
)
async def test_failed_checks_never_pass(provider, fault):
    factory = WireFactory(fault=fault)
    report = await runner.run_smoke(
        config(provider, scenario="tool"), allow_network=True, provider_factory=factory
    )
    assert report.reason in ("runtime_failed", "check_failed"), report
    assert 1 <= len(factory.requests) <= 2
    assert report.replay_verified
    assert factory.closed and all(w.closed for w in factory.wires)
    assert CANARY not in report.model_dump_json()


async def test_token_budget_stops_followup_request(provider):
    factory = WireFactory()
    report = await runner.run_smoke(
        config(provider, scenario="tool", max_tokens=1),
        allow_network=True,
        provider_factory=factory,
    )
    assert report.reason == "runtime_failed"
    assert report.failure_category == "budget"
    assert len(factory.requests) == 1
    assert (
        factory.requests[0].get("max_tokens", factory.requests[0].get("max_completion_tokens")) == 1
    )
    assert factory.closed and all(w.closed for w in factory.wires)


async def test_transport_timeout_closes_without_retries(provider):
    factory = WireFactory(fault="timeout")
    report = await runner.run_smoke(config(provider), allow_network=True, provider_factory=factory)
    assert report.reason == "runtime_failed", report
    assert report.failure_category == "provider"
    assert report.provider_failure is not None and report.provider_failure.code == "transport"
    assert len(factory.requests) == 1 and factory.wires[0].closed and factory.closed
    assert report.replay_verified


async def test_task_cancel_propagates_and_cleans_up(provider):
    factory = WireFactory(fault="cancel")
    task = asyncio.create_task(
        runner.run_smoke(config(provider), allow_network=True, provider_factory=factory)
    )
    async with asyncio.timeout(5):
        await factory.request_entered.wait()
        await factory.wires[0].entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert factory.wires[0].closed and factory.closed


async def test_public_report_does_not_copy_hostile_response_metadata(provider):
    factory = WireFactory(fault="hostile_metadata")
    report = await runner.run_smoke(config(provider), allow_network=True, provider_factory=factory)
    assert report.reason == "passed"
    assert CANARY not in report.model_dump_json()


async def test_replay_mismatch_prevents_pass(provider, monkeypatch):
    factory = WireFactory()
    monkeypatch.setattr(runner, "replay", lambda _: None)
    report = await runner.run_smoke(config(provider), allow_network=True, provider_factory=factory)
    assert report.reason == "check_failed" and not report.replay_verified
    assert report.usage_complete and report.content_verified


@pytest.mark.parametrize(
    "error,reason",
    [
        (ModuleNotFoundError(CANARY), "dependency_missing"),
        (ValueError(CANARY), "configuration_invalid"),
        (RuntimeError(CANARY), "internal_error"),
    ],
)
async def test_factory_error_sanitized_with_unknown_consumption(error, reason):
    def factory(_):
        raise error

    report = await runner.run_smoke(config(), allow_network=True, provider_factory=factory)
    assert report.reason == reason
    assert report.attempts_started is None and report.known_input_tokens is None
    assert CANARY not in report.model_dump_json()


@pytest.mark.parametrize(
    "change",
    [
        {"max_output_tokens": 513},
        {"max_output_tokens": True},
        {"max_tokens": "1"},
        {"timeout_seconds": 61},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"api_key": CANARY},
        {"api_key_env": "bad-name"},
        {"base_url": "http://smoke.invalid"},
        {"base_url": "https://u:p@smoke.invalid"},
        {"base_url": "https://smoke.invalid/?key=secret"},
        {"base_url": "https://smoke.invalid/#secret"},
        {"base_url": "https://smoke.invalid:999999"},
        {"scenario": "shell"},
        {"prompt": CANARY},
        {"max_attempts": 2},
    ],
)
def test_config_closed_and_strict(change):
    data = config().model_dump()
    data.update(change)
    with pytest.raises(ValueError):
        SmokeConfig.model_validate(data)


def test_provider_specific_parameters_and_fixed_bounds():
    with pytest.raises(ValueError):
        config("anthropic", output_token_parameter="max_tokens")
    cfg = config(output_token_parameter="max_tokens").provider_config()
    assert cfg.output_token_parameter == "max_tokens"
    assert cfg.max_attempts == 1 and cfg.retry_delay_seconds == 0
    assert cfg.max_request_bytes == 65536 and cfg.max_response_bytes == 524288
    assert cfg.max_frame_bytes == 65536 and cfg.max_chunks == 2048
    assert config().budget().max_steps == 1
    assert config(scenario="approval").budget().max_steps == 2


def test_report_cannot_claim_pass_without_evidence():
    with pytest.raises(ValueError):
        SmokeReport(reason="passed")
