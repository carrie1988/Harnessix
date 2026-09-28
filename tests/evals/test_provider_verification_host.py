"""有限验证宿主接线；仅使用自有临时账本与实际Adapter的MockTransport。"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteRunReport
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_suite import build_task_pack_suite_config
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from scripts import run_engineering_provider_suite_budgeted as host
from tests.contracts.provider import model_request
from tests.evals.provider_suite_helpers import provider_suite_config
from tests.evals.test_provider_verification_budget import PERIOD, bounds, ledger_file, period
from tests.models.wire import WireStream, chunk, frame, response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="当前验证宿主限定POSIX")


def live_config(tmp_path):
    original = provider_suite_config(tmp_path)
    now = datetime.now(UTC)
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    price = original.suite.campaign_plans[0].price.model_copy(
        update={"valid_from": now - timedelta(minutes=1), "valid_until": now + timedelta(hours=1)}
    )
    suite = build_task_pack_suite_config(
        loaded,
        suite_id=original.suite.plan.suite_id,
        work_root=tmp_path / "private-suite",
        environment=CodingEvalEnvironment(
            harnessix_revision="a" * 40,
            provider="openai_chat",
            model=host.MODEL,
            platform="linux",
            isolation="fixed-container-checks-provider-network",
        ),
        price=price,
        billing_context=original.suite.campaign_plans[0].billing_context,
        fee_stop_amount="40",
        created_at=now,
    )
    return original.model_copy(update={"suite": suite})


def forbidden(*_, **__):
    pytest.fail("该前置拒绝路径不得读取凭据或发出请求")


def test_default_cli_never_reads_config_budget_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr(host, "read_private_eval_config", forbidden)
    monkeypatch.setattr(host, "_credential", forbidden)
    monkeypatch.setattr(host, "VerificationBudgetLedger", forbidden)
    with pytest.raises(SystemExit) as exit_result:
        host.main(
            [
                "--config",
                "/missing/private.json",
                "--budget-ledger",
                "/missing/budget.json",
                "--period-id",
                str(PERIOD),
            ]
        )
    assert exit_result.value.code == 2
    assert json.loads(capsys.readouterr().out) == {"reason": "network_not_enabled"}


def test_invalid_cli_does_not_echo_argument_body(capsys):
    with pytest.raises(SystemExit):
        host.main(["--not-supported", "fixture-private-canary"])
    output = capsys.readouterr()
    assert "fixture-private-canary" not in output.out + output.err


async def test_direct_host_is_default_disabled_before_config_access(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "_bounds", forbidden)
    with pytest.raises(KernelError) as failed:
        await host.run_budgeted_suite(None, budget_path=tmp_path / "none", period_id=PERIOD)
    assert failed.value.code == "eval_provider_suite_network_disabled"


@pytest.mark.parametrize("case", ["endpoint", "model", "output_parameter", "pack", "expired_price"])
async def test_fixed_scope_or_price_rejection_precedes_program_or_credential_access(
    tmp_path, monkeypatch, case
):
    config = live_config(tmp_path)
    if case == "expired_price":
        config = provider_suite_config(tmp_path)
    elif case == "pack":
        config = config.model_copy(update={"pack_version": 1})
    else:
        update = {
            "endpoint": {"base_url": "https://other.example/v1"},
            "model": {"model": "other-model"},
            "output_parameter": {"output_token_parameter": "max_completion_tokens"},
        }[case]
        config = config.model_copy(
            update={"provider_config": config.provider_config.model_copy(update=update)}
        )
    monkeypatch.setattr(host, "_require_scope", forbidden)
    monkeypatch.setattr(host, "_credential", forbidden)
    with pytest.raises((KernelError, ValueError)):
        await host.run_budgeted_suite(
            config, budget_path=tmp_path / "none", period_id=PERIOD, allow_network=True
        )


async def test_source_mismatch_and_missing_image_never_touch_credentials_or_ledger(
    tmp_path, monkeypatch
):
    config = live_config(tmp_path)
    monkeypatch.setattr(host, "_credential", forbidden)
    monkeypatch.setattr(host, "VerificationBudgetLedger", forbidden)

    def wrong_source(*_):
        raise KernelError("eval_provider_suite_source_revision_mismatch", "fixture")

    monkeypatch.setattr(host, "_require_scope", wrong_source)
    with pytest.raises(KernelError) as failed:
        await host.run_budgeted_suite(
            config, budget_path=tmp_path / "none", period_id=PERIOD, allow_network=True
        )
    assert failed.value.code == "eval_provider_suite_source_revision_mismatch"
    monkeypatch.setattr(host, "_require_scope", lambda *_: None)
    commands = []

    def missing_image(command, **_):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(host.subprocess, "run", missing_image)
    with pytest.raises(KernelError) as failed:
        await host.run_budgeted_suite(
            config, budget_path=tmp_path / "none", period_id=PERIOD, allow_network=True
        )
    assert failed.value.code == "verification_image_unavailable"
    assert len(commands) == 1 and commands[0][1:3] == ("image", "inspect")


async def test_unresolved_budget_rejection_precedes_credentials(tmp_path, monkeypatch):
    path = ledger_file(tmp_path)
    with host.VerificationBudgetLedger(path, PERIOD) as ledger:
        ledger.reserve(bounds().maximum_units, {})
    before = path.read_bytes()
    monkeypatch.setattr(host, "_require_scope", lambda *_: None)
    monkeypatch.setattr(host, "_require_images", lambda *_: None)
    monkeypatch.setattr(host, "_credential", forbidden)
    with pytest.raises(KernelError) as failed:
        await host.run_budgeted_suite(
            live_config(tmp_path), budget_path=path, period_id=PERIOD, allow_network=True
        )
    assert failed.value.code == "verification_budget_unresolved" and path.read_bytes() == before


async def test_host_factory_runs_native_adapter_and_binds_original_runner(tmp_path, monkeypatch):
    path = ledger_file(tmp_path)
    config = live_config(tmp_path)
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(host, "_require_scope", lambda *_: None)
    monkeypatch.setattr(host, "_require_images", lambda *_: None)
    monkeypatch.setattr(host, "_credential", lambda *_: "fixture-SECRET-CANARY")
    parts = [
        chunk({"role": "assistant", "content": "fixture"}),
        chunk(finish="stop"),
        chunk(usage=True),
    ]
    for part in parts:
        part["model"] = host.MODEL
    wire = WireStream([frame(part) for part in parts] + [b"data: [DONE]\n\n"])

    def transport(request):
        assert period(path)["requests"][-1]["status"] == "reserved"
        assert json.loads(request.content)["max_tokens"] == 4096
        return response(wire)

    def native(provider_config, *, api_key):
        assert api_key == "fixture-SECRET-CANARY"
        return OpenAIChatProvider(
            provider_config, api_key=api_key, transport=httpx.MockTransport(transport)
        )

    monkeypatch.setattr(host, "OpenAIChatProvider", native)

    async def runner(checked, **kwargs):
        assert checked == config and kwargs["allow_network"] is True and kwargs["resume"] is True
        assert len(kwargs["provider_binding_sha256"]) == 64
        async with kwargs["provider_factory"](None, UUID(int=1)) as provider:
            events = [event async for event in provider.stream(model_request(), CancelToken())]
            assert isinstance(events[-1], ResponseCompleted)
            assert not kwargs["cancel"].cancelled
        return CodingEvalSuiteRunReport(
            reason="completed",
            suite_id=checked.suite.plan.suite_id,
            scheduled_cases=10,
            completed_cases=10,
            report_published=True,
            known_cost_currency="CNY",
            known_cost_amount="0.000072",
        )

    monkeypatch.setattr(host, "run_task_pack_provider_suite", runner)
    result = await host.run_budgeted_suite(
        config, budget_path=path, period_id=PERIOD, allow_network=True, resume=True
    )
    assert result.reason == "completed" and wire.closed
    assert period(path)["known_cost"] == "0.00014" and period(path)["allocation"] == "70"


def test_keychain_failure_does_not_fall_back_and_launchctl_precedes_parent_env(
    tmp_path, monkeypatch
):
    config = live_config(tmp_path)
    monkeypatch.setattr(host.sys, "platform", "darwin")
    monkeypatch.setenv(config.provider_config.api_key_env, "parent-fixture-key")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    commands = []

    def process(command, **_):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1 if command[0] == "/usr/bin/security" else 0,
            stdout=b"launchctl-fixture-key\n",
            stderr=b"",
        )

    monkeypatch.setattr(host.subprocess, "run", process)
    assert host._credential(config, None, None) == "launchctl-fixture-key"
    with pytest.raises(KernelError) as failed:
        host._credential(config, "com.example.fixture", "verification")
    assert failed.value.code == "verification_credentials_unavailable"
    assert len(commands) == 2 and commands[0][0] == "/bin/launchctl"


def test_main_failure_does_not_publish_raw_exception_or_fake_zero_cost(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        host, "read_private_eval_config", lambda *_args, **_kwargs: live_config(tmp_path)
    )

    async def failed(*_, **__):
        raise RuntimeError("fixture-private-canary")

    monkeypatch.setattr(host, "run_budgeted_suite", failed)
    with pytest.raises(SystemExit) as stopped:
        host.main(
            [
                "--config",
                "/missing",
                "--budget-ledger",
                "/missing",
                "--period-id",
                str(PERIOD),
                "--allow-network",
            ]
        )
    assert stopped.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"reason": "verification_host_failed"} and not output.err
