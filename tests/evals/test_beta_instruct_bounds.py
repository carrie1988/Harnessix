"""Beta Instruct边界回归：真实Guard、内存Provider和临时账本，不访问外部服务。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from scripts.provider_verification_guard import (
    MODEL,
    PRICE_SOURCE,
    BailianBetaInstructVerificationBounds,
    BailianBetaVerificationBounds,
    BailianVerificationBounds,
    GuardedVerificationProvider,
)
from tests.evals.test_provider_reverification_task import authorized_task, task_scope
from tests.evals.test_provider_reverification_task import isolated_io as isolated_io
from tests.evals.test_provider_verification_budget import model_request, period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")
INSTRUCT_MODEL = "qwen3-235b-a22b-instruct-2507"
INSTRUCT_PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/qwen3-235b-a22b-instruct-2507"
SUITE_ID = UUID("22222222-2222-4222-8222-222222222222")


def instruct_bounds(output=3072):
    now = datetime.now(UTC)
    return BailianBetaInstructVerificationBounds(
        now - timedelta(minutes=1), now + timedelta(hours=1), output
    )


@dataclass
class MockProvider:
    path: Path
    usage: UsageObservation
    requested_model: str = INSTRUCT_MODEL
    actual_model: str | None = INSTRUCT_MODEL
    calls: int = 0
    closed: bool = False

    async def stream(self, request, cancel):
        self.calls += 1
        try:
            latest = period(self.path)["requests"][-1]
            assert latest["status"] == "reserved"
            assert latest["reserved_cost"] == "0.282624"
            cancel.checkpoint()
            attempt = uuid4()
            yield ModelAttemptStarted(
                attempt_id=attempt,
                step=request.step,
                index=1,
                provider="openai_chat",
                requested_model=self.requested_model,
            )
            yield ModelUsageObserved(
                attempt_id=attempt, usage=self.usage, actual_model=self.actual_model
            )
            yield ModelAttemptFinished(attempt_id=attempt, outcome="completed")
            yield ResponseCompleted(
                usage=Usage(
                    input_tokens=self.usage.input_tokens or 0,
                    output_tokens=self.usage.output_tokens or 0,
                )
            )
        finally:
            self.closed = True


@pytest.mark.parametrize(
    "input_tokens,output_tokens,expected",
    [
        (0, 0, "0"),
        (0, 1, "0.000008"),
        (1, 0, "0.000002"),
        (13, 1, "0.000034"),
        (32_000, 1, "0.064008"),
        (32_001, 1, "0.06401"),
        (128_000, 1, "0.256008"),
        (128_001, 1, "0.25601"),
        (129_024, 3072, "0.282624"),
    ],
)
def test_instruct_uses_one_exact_price_tier(input_tokens, output_tokens, expected):
    usage = UsageObservation(
        completeness="complete", input_tokens=input_tokens, output_tokens=output_tokens
    )
    assert instruct_bounds().cost_units(usage) == amount_units(expected)


@pytest.mark.parametrize(
    "output,expected", [(1, "0.258056"), (3072, "0.282624"), (4096, "0.290816")]
)
def test_reservation_uses_full_input_and_host_output_limit(output, expected):
    bounds = instruct_bounds(output)
    bounds.checkpoint()
    assert isinstance(bounds, BailianBetaVerificationBounds)
    assert bounds.model == INSTRUCT_MODEL
    assert bounds.price_source == INSTRUCT_PRICE_SOURCE
    assert bounds.purpose == "beta-001-model-request"
    assert bounds.guard_version == "harnessix.bailian-beta-instruct-request-guard/v1"
    assert bounds.maximum_input_tokens == 129_024
    assert bounds.price_tiers == ((129_024, 2_000, 8_000),)
    assert bounds.maximum_units == amount_units(expected)


@pytest.mark.parametrize("output", [True, 0, -1, 3072.0, 4097, 32_768])
async def test_invalid_host_output_bound_is_rejected_before_call(tmp_path, output):
    path, plan = authorized_task(tmp_path)
    before = path.read_bytes()
    provider = MockProvider(path, UsageObservation())
    token = CancelToken()
    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(provider, ledger, instruct_bounds(output), token)
        with pytest.raises(KernelError) as failed:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert failed.value.code == "verification_price_unavailable"
    assert provider.calls == 0 and token.cancelled
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "input_tokens,output_tokens,expected",
    [(0, 0, "0"), (13, 1, "0.000034"), (129_024, 3072, "0.282624")],
)
async def test_complete_usage_settles_before_success_and_preserves_history(
    tmp_path, input_tokens, output_tokens, expected
):
    path, plan = authorized_task(tmp_path)
    before = period(path)
    usage = UsageObservation(
        completeness="complete", input_tokens=input_tokens, output_tokens=output_tokens
    )
    provider = MockProvider(path, usage)
    token = CancelToken()
    seen = []
    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(provider, ledger, instruct_bounds(), token)
        async for event in guard.stream(model_request(), CancelToken()):
            seen.append(event)
            if isinstance(event, ResponseCompleted):
                latest = period(path)["requests"][-1]
                assert latest["status"] == "completed"
                assert latest["reserved_cost"] == "0"
                assert latest["cost_estimate"] == expected
        ledger.require_available()
    after = period(path)
    latest = after["requests"][-1]
    assert isinstance(seen[-1], ResponseCompleted)
    assert provider.calls == 1 and provider.closed and not token.cancelled
    assert after["requests"][:-1] == before["requests"]
    assert after["reserved_cost"] == before["reserved_cost"]
    assert amount_units(after["known_cost"]) == (
        amount_units(before["known_cost"]) + amount_units(expected)
    )
    assert latest["requested_model"] == INSTRUCT_MODEL
    assert latest["price_basis"] == INSTRUCT_PRICE_SOURCE
    assert latest["purpose"] == "beta-001-model-request"
    assert latest["task_id"] == "BETA-001"
    assert latest["max_output_tokens"] == 3072 and latest["max_attempts"] == 1
    assert latest["region"] == "cn-beijing"
    assert json.loads(path.read_text())["schema"] == "harnessix.provider-verification-budget/v1"


@pytest.mark.parametrize(
    "usage,requested_model,actual_model,error",
    [
        (
            UsageObservation(completeness="complete", input_tokens=129_025, output_tokens=1),
            INSTRUCT_MODEL,
            INSTRUCT_MODEL,
            "verification_request_cost_unknown",
        ),
        (
            UsageObservation(completeness="complete", input_tokens=10, output_tokens=3073),
            INSTRUCT_MODEL,
            INSTRUCT_MODEL,
            "verification_request_cost_unknown",
        ),
        (
            UsageObservation(completeness="partial", input_tokens=10, output_tokens=1),
            INSTRUCT_MODEL,
            INSTRUCT_MODEL,
            "verification_request_cost_unknown",
        ),
        (UsageObservation(), INSTRUCT_MODEL, INSTRUCT_MODEL, "verification_request_cost_unknown"),
        (
            UsageObservation(completeness="complete", input_tokens=10, output_tokens=1),
            INSTRUCT_MODEL,
            "qwen3-coder-next",
            "verification_request_cost_unknown",
        ),
        (
            UsageObservation(completeness="complete", input_tokens=10, output_tokens=1),
            INSTRUCT_MODEL,
            None,
            "verification_request_cost_unknown",
        ),
        (
            UsageObservation(completeness="complete", input_tokens=10, output_tokens=1),
            "qwen3-coder-next",
            INSTRUCT_MODEL,
            "verification_provider_protocol_invalid",
        ),
    ],
    ids=["input_overflow", "output_overflow", "partial", "unknown", "alias", "no_model", "request"],
)
async def test_invalid_usage_or_model_keeps_full_hold_and_stops(
    tmp_path, usage, requested_model, actual_model, error
):
    path, plan = authorized_task(tmp_path)
    before = period(path)
    provider = MockProvider(path, usage, requested_model, actual_model)
    token = CancelToken()
    seen = []
    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(provider, ledger, instruct_bounds(), token)
        with pytest.raises(KernelError) as failed:
            async for event in guard.stream(model_request(), CancelToken()):
                seen.append(event)
        assert failed.value.code == error
        with pytest.raises(KernelError, match="验证预算存在未决请求"):
            ledger.require_available()
        with pytest.raises(TurnCancelled):
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
    after = period(path)
    assert provider.calls == 1 and provider.closed and token.cancelled
    assert not any(isinstance(event, ResponseCompleted) for event in seen)
    assert after["requests"][:-1] == before["requests"]
    assert after["requests"][-1]["status"] == "unknown"
    assert after["requests"][-1]["reserved_cost"] == "0.282624"
    assert "cost_estimate" not in after["requests"][-1]
    assert after["known_cost"] == before["known_cost"]
    assert amount_units(after["reserved_cost"]) == (
        amount_units(before["reserved_cost"]) + amount_units("0.282624")
    )


@pytest.mark.parametrize(
    "task_id,suite_id",
    [(None, None), ("BETA-002", None), (None, SUITE_ID), ("BETA-001", SUITE_ID)],
)
async def test_wrong_task_or_suite_is_rejected_without_call_or_reservation(
    tmp_path, monkeypatch, task_id, suite_id
):
    path, plan = authorized_task(tmp_path)
    before = path.read_bytes()
    provider = MockProvider(path, UsageObservation())
    token = CancelToken()
    with task_scope(path, plan) as ledger:
        # 在真实Owner打开后注入错误身份，独立验证Guard而非仅验证Ledger入口。
        monkeypatch.setattr(ledger, "task_id", task_id)
        monkeypatch.setattr(ledger, "suite_id", suite_id)
        guard = GuardedVerificationProvider(provider, ledger, instruct_bounds(), token)
        with pytest.raises(KernelError, match="Beta模型只属于单任务授权") as failed:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert failed.value.code == "verification_budget_unresolved"
    assert provider.calls == 0 and token.cancelled
    assert path.read_bytes() == before


@pytest.mark.parametrize("spent,allowed", [("4.717376", True), ("4.717376000000000001", False)])
async def test_remaining_budget_requires_full_reservation(tmp_path, spent, allowed):
    path, plan = authorized_task(tmp_path)
    provider = MockProvider(
        path, UsageObservation(completeness="complete", input_tokens=10, output_tokens=1)
    )
    token = CancelToken()
    with task_scope(path, plan) as ledger:
        previous = ledger.reserve(amount_units(spent), {})
        ledger.settle(previous, amount_units(spent), sent=True)
        before = path.read_bytes()
        guard = GuardedVerificationProvider(provider, ledger, instruct_bounds(), token)
        if allowed:
            seen = [event async for event in guard.stream(model_request(), CancelToken())]
            assert isinstance(seen[-1], ResponseCompleted)
        else:
            with pytest.raises(KernelError) as failed:
                _ = [event async for event in guard.stream(model_request(), CancelToken())]
            assert failed.value.code == "verification_budget_exhausted"
            assert path.read_bytes() == before
    assert provider.calls == int(allowed) and token.cancelled == (not allowed)


def test_independent_fingerprint_preserves_r3_and_next_configuration(tmp_path):
    path, plan = authorized_task(tmp_path)
    before = path.read_bytes()
    instruct = instruct_bounds()
    r3 = BailianVerificationBounds(instruct.valid_from, instruct.valid_until, 3072)
    next_model = BailianBetaVerificationBounds(instruct.valid_from, instruct.valid_until, 3072)
    assert r3.model == MODEL == "qwen3-coder-plus-2025-09-23"
    assert (
        r3.price_source
        == PRICE_SOURCE
        == "https://help.aliyun.com/zh/model-studio/qwen3-coder-plus"
    )
    assert r3.guard_version == "harnessix.bailian-verification-request-guard/v1"
    assert r3.purpose == "engineering-suite-model-request"
    assert r3.maximum_input_tokens == 997_952
    assert r3.price_tiers == (
        (32_000, 4_000, 16_000),
        (128_000, 6_000, 24_000),
        (256_000, 10_000, 40_000),
        (1_000_000, 20_000, 200_000),
    )
    assert r3.maximum_units == amount_units("20.57344")
    assert next_model.model == "qwen3-coder-next"
    assert next_model.price_source == "https://help.aliyun.com/zh/model-studio/qwen3-coder-next"
    assert next_model.guard_version == "harnessix.bailian-beta-request-guard/v1"
    assert next_model.purpose == "beta-001-model-request"
    assert next_model.maximum_input_tokens == 204_800
    assert next_model.price_tiers == (
        (32_000, 1_000, 4_000),
        (128_000, 1_500, 6_000),
        (262_144, 2_500, 10_000),
    )
    assert next_model.maximum_units == amount_units("0.54272")
    with task_scope(path, plan) as ledger:
        fingerprints = {bounds.fingerprint(ledger) for bounds in (r3, next_model, instruct)}
        assert len(fingerprints) == 3
        same = BailianBetaInstructVerificationBounds(
            instruct.valid_from, instruct.valid_until, 3072
        )
        assert same.fingerprint(ledger) == instruct.fingerprint(ledger)
    assert path.read_bytes() == before
