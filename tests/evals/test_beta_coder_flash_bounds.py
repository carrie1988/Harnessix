"""Beta Coder Flash离线边界；复用临时账本与Provider，禁止网络和真实凭据。"""

from __future__ import annotations

import os
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Usage
from harnessix.agent.usage import ModelAttemptStarted, ModelUsageObserved, UsageObservation
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import (
    MODEL,
    BailianBetaCoderFlashVerificationBounds,
    BailianBetaInstructVerificationBounds,
    BailianBetaVerificationBounds,
    BailianVerificationBounds,
    GuardedVerificationProvider,
)
from tests.evals.test_beta_task_budget_plan import authorized_budget
from tests.evals.test_provider_reverification import held_ledger, plan_for, scoped
from tests.evals.test_provider_reverification_task import authorized_task, task_scope
from tests.evals.test_provider_reverification_task import isolated_io as isolated_io
from tests.evals.test_provider_verification_budget import ObservedProvider, model_request, period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")
FLASH_MODEL = "qwen3-coder-flash-2025-07-28"


def flash_bounds(output=3072):
    now = datetime.now(UTC)
    return BailianBetaCoderFlashVerificationBounds(
        now - timedelta(minutes=1), now + timedelta(hours=1), output
    )


class FlashProvider(ObservedProvider):
    """只适配存量离线事件的模型/用量；预留先行断言与关闭逻辑仍复用原helper。"""

    def __init__(self, path, *, usage=None, mode="complete", actual_model=FLASH_MODEL):
        super().__init__(path, mode)
        self.usage = (
            usage
            if usage is not None
            else UsageObservation(completeness="complete", input_tokens=13, output_tokens=1)
        )
        self.actual_model = actual_model

    async def stream(self, request, cancel):
        self.reservation = period(self.path)["requests"][-1]
        async with aclosing(super().stream(request, cancel)) as events:
            async for event in events:
                if isinstance(event, ModelAttemptStarted):
                    event = event.model_copy(update={"requested_model": FLASH_MODEL})
                elif isinstance(event, ModelUsageObserved):
                    event = event.model_copy(
                        update={"usage": self.usage, "actual_model": self.actual_model}
                    )
                elif isinstance(event, ResponseCompleted):
                    event = event.model_copy(
                        update={
                            "usage": Usage(
                                input_tokens=self.usage.input_tokens or 0,
                                output_tokens=self.usage.output_tokens or 0,
                            )
                        }
                    )
                yield event


@pytest.mark.parametrize(
    "input_tokens,expected",
    [
        (0, "0.000004"),
        (31_999, "0.032003"),
        (32_000, "0.032004"),
        (32_001, "0.0480075"),
        (127_999, "0.1920045"),
        (128_000, "0.192006"),
        (128_001, "0.3200125"),
        (255_999, "0.6400075"),
        (256_000, "0.64001"),
        (256_001, "1.28003"),
        (997_952, "4.989785"),
    ],
)
def test_all_four_tier_edges_charge_exact_undiscounted_rates(input_tokens, expected):
    usage = UsageObservation(
        completeness="complete",
        input_tokens=input_tokens,
        output_tokens=1,
        uncached_input_tokens=0,
        cache_read_input_tokens=input_tokens,
        cache_creation_input_tokens=0,
    )
    assert flash_bounds().cost_units(usage) == amount_units(expected)


@pytest.mark.parametrize("output,expected", [(1, "4.989785"), (3072, "5.06656"), (4096, "5.09216")])
def test_full_input_reservation_and_configuration_only_inheritance(output, expected):
    bounds = flash_bounds(output)
    bounds.checkpoint()
    assert isinstance(bounds, BailianBetaVerificationBounds)
    assert bounds.model == FLASH_MODEL
    assert bounds.price_source == "https://help.aliyun.com/zh/model-studio/qwen3-coder-flash"
    assert bounds.guard_version == "harnessix.bailian-beta-coder-flash-request-guard/v1"
    assert bounds.maximum_input_tokens == 997_952
    assert bounds.price_tiers == (
        (32_000, 1_000, 4_000),
        (128_000, 1_500, 6_000),
        (256_000, 2_500, 10_000),
        (1_000_000, 5_000, 25_000),
    )
    assert bounds.maximum_units == amount_units(expected)
    assert (
        bounds.cost_units(
            UsageObservation(
                completeness="complete",
                input_tokens=997_952,
                output_tokens=output,
            )
        )
        == bounds.maximum_units
    )
    assert bounds.purpose == "beta-001-model-request"
    for name in ("purpose", "checkpoint", "maximum_units", "cost_units", "fingerprint"):
        assert name not in BailianBetaCoderFlashVerificationBounds.__dict__


@pytest.mark.parametrize("output", [True, 0, -1, 3072.0, 4097, 65_536])
async def test_host_output_limit_is_not_increased(tmp_path, output):
    path, plan = authorized_budget(tmp_path)
    before = path.read_bytes()
    provider, token = FlashProvider(path), CancelToken()
    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(provider, ledger, flash_bounds(output), token)
        with pytest.raises(KernelError) as caught:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert caught.value.code == "verification_price_unavailable"
    assert provider.sent == 0 and token.cancelled and path.read_bytes() == before


@pytest.mark.parametrize("output,reserved", [(3072, "5.06656"), (4096, "5.09216")])
@pytest.mark.parametrize("full_input", [False, True])
async def test_sixty_yuan_period_beta_ten_reserves_full_input_before_settling(
    tmp_path, output, reserved, full_input
):
    path, plan = authorized_budget(tmp_path)
    before = period(path)
    usage = UsageObservation(
        completeness="complete",
        input_tokens=997_952 if full_input else 13,
        output_tokens=output if full_input else 1,
    )
    provider, token = FlashProvider(path, usage=usage), CancelToken()
    expected = reserved if full_input else "0.000017"
    with task_scope(path, plan) as ledger:
        assert ledger.allocation == amount_units("60") and plan.maximum_cost == "10"
        guard = GuardedVerificationProvider(provider, ledger, flash_bounds(output), token)
        async for event in guard.stream(model_request(), CancelToken()):
            if isinstance(event, ResponseCompleted):
                latest = period(path)["requests"][-1]
                assert latest["status"] == "completed" and latest["reserved_cost"] == "0"
                assert latest["cost_estimate"] == expected
        assert isinstance(event, ResponseCompleted)
        ledger.require_available()
    assert provider.sent == 1 and provider.closed and not token.cancelled
    assert provider.reservation["reserved_cost"] == reserved
    assert provider.reservation["requested_model"] == FLASH_MODEL
    assert provider.reservation["purpose"] == "beta-001-model-request"
    assert provider.reservation["task_id"] == "BETA-001"
    after = period(path)
    assert after["requests"][:-1] == before["requests"]
    assert after["reserved_cost"] == "0" and after["known_cost"] == expected


@pytest.mark.parametrize("output", [3072, 4096])
async def test_old_five_yuan_authorization_rejects_without_altering_history(tmp_path, output):
    path, plan = authorized_task(tmp_path)
    before = path.read_bytes()
    provider, token = FlashProvider(path), CancelToken()
    with task_scope(path, plan) as ledger:
        assert plan.maximum_cost == "5" and ledger.allocation == amount_units("60")
        guard = GuardedVerificationProvider(provider, ledger, flash_bounds(output), token)
        with pytest.raises(KernelError) as caught:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert caught.value.code == "verification_budget_exhausted"
    assert provider.sent == 0 and token.cancelled and path.read_bytes() == before


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "unknown",
        "partial",
        "input_overflow",
        "price_ceiling",
        "past_price_ceiling",
        "output_overflow",
        "wrong_model",
        "no_model",
    ],
)
async def test_new_unknown_keeps_full_hold_and_blocks_next_request_and_reopen(tmp_path, case):
    path, plan = authorized_budget(tmp_path)
    before = period(path)
    usage = UsageObservation(completeness="complete", input_tokens=13, output_tokens=1)
    if case == "unknown":
        usage = UsageObservation()
    elif case == "partial":
        usage = UsageObservation(completeness="partial", input_tokens=13)
    elif case in {"input_overflow", "price_ceiling", "past_price_ceiling"}:
        usage = UsageObservation(
            completeness="complete",
            output_tokens=1,
            input_tokens={
                "input_overflow": 997_953,
                "price_ceiling": 1_000_000,
                "past_price_ceiling": 1_000_001,
            }[case],
        )
    elif case == "output_overflow":
        usage = UsageObservation(completeness="complete", input_tokens=13, output_tokens=3073)
    actual_model = {"no_model": None, "wrong_model": MODEL}.get(case, FLASH_MODEL)
    provider = FlashProvider(path, usage=usage, mode=case, actual_model=actual_model)
    token, seen = CancelToken(), []
    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(provider, ledger, flash_bounds(), token)
        with pytest.raises(KernelError) as caught:
            async for event in guard.stream(model_request(), CancelToken()):
                seen.append(event)
        assert caught.value.code == "verification_request_cost_unknown"
        with pytest.raises(KernelError) as blocked:
            ledger.require_available()
        assert blocked.value.code == "verification_budget_unresolved"
        with pytest.raises(TurnCancelled):
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
    assert provider.sent == 1 and provider.closed and token.cancelled
    assert not any(isinstance(event, ResponseCompleted) for event in seen)
    after = period(path)
    assert after["requests"][:-1] == before["requests"]
    assert after["known_cost"] == before["known_cost"]
    assert after["requests"][-1]["status"] == "unknown"
    assert after["reserved_cost"] == after["requests"][-1]["reserved_cost"] == "5.06656"
    saved = path.read_bytes()
    with pytest.raises(KernelError) as blocked:
        with task_scope(path, plan):
            pytest.fail("new unknown must block reopen")
    assert blocked.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == saved


@pytest.mark.parametrize("scope", ["unscoped", "wrong_task", "mixed", "r3"])
async def test_wrong_task_and_r3_suite_cannot_reserve_or_send(tmp_path, monkeypatch, scope):
    if scope == "r3":
        path = held_ledger(tmp_path)
        plan = plan_for(path)
        VerificationBudgetLedger.authorize_reverification(path, plan)
        owner = scoped(path, plan)
    else:
        path, plan = authorized_budget(tmp_path)
        owner = task_scope(path, plan)
    before = path.read_bytes()
    provider, token = FlashProvider(path), CancelToken()
    with owner as ledger:
        if scope != "r3":
            task_id = {"unscoped": None, "wrong_task": "BETA-002", "mixed": "BETA-001"}[scope]
            monkeypatch.setattr(ledger, "task_id", task_id)
            if scope == "mixed":
                monkeypatch.setattr(ledger, "suite_id", uuid4())
        guard = GuardedVerificationProvider(provider, ledger, flash_bounds(), token)
        with pytest.raises(KernelError) as caught:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert caught.value.code == "verification_budget_unresolved"
    assert provider.sent == 0 and token.cancelled and path.read_bytes() == before


def test_flash_fingerprint_is_distinct_without_changing_r3_next_or_instruct(tmp_path):
    path, plan = authorized_task(tmp_path)
    before = path.read_bytes()
    flash = flash_bounds()
    old = [
        kind(flash.valid_from, flash.valid_until, 3072)
        for kind in (
            BailianVerificationBounds,
            BailianBetaVerificationBounds,
            BailianBetaInstructVerificationBounds,
        )
    ]
    assert MODEL == old[0].model == "qwen3-coder-plus-2025-09-23"
    assert [bounds.model for bounds in old[1:]] == [
        "qwen3-coder-next",
        "qwen3-235b-a22b-instruct-2507",
    ]
    assert [bounds.maximum_units for bounds in old] == [
        amount_units(value) for value in ("20.57344", "0.54272", "0.282624")
    ]
    with task_scope(path, plan) as ledger:
        assert len({bounds.fingerprint(ledger) for bounds in (*old, flash)}) == 4
    assert path.read_bytes() == before
