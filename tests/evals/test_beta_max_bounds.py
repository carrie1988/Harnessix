"""Beta Max离线边界；仅使用存量隔离fixture、内存Provider及临时账本。"""

from contextlib import aclosing
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.usage import ModelAttemptStarted, UsageObservation
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from scripts.provider_verification_guard import (
    BailianBetaMaxVerificationBounds,
    BailianBetaVerificationBounds,
    GuardedVerificationProvider,
)
from tests.evals.test_beta_coder_flash_bounds import FlashProvider, flash_bounds
from tests.evals.test_beta_coder_flash_bounds import pytestmark as pytestmark
from tests.evals.test_beta_task_budget_plan import authorized_budget
from tests.evals.test_provider_reverification_task import isolated_io as isolated_io
from tests.evals.test_provider_reverification_task import task_scope
from tests.evals.test_provider_verification_budget import model_request, period

MAX_MODEL = "qwen3-max-2025-09-23"


def max_bounds(output=3072):
    window = flash_bounds(output)
    return BailianBetaMaxVerificationBounds(window.valid_from, window.valid_until, output)


class MaxProvider(FlashProvider):
    async def stream(self, request, cancel):
        async with aclosing(super().stream(request, cancel)) as events:
            async for event in events:
                if isinstance(event, ModelAttemptStarted):
                    event = event.model_copy(update={"requested_model": MAX_MODEL})
                yield event


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (0, "0.000024"),
        (31_999, "0.192018"),
        (32_000, "0.192024"),
        (32_001, "0.32005"),
        (127_999, "1.28003"),
        (128_000, "1.28004"),
        (128_001, "1.920075"),
        (258_048, "3.87078"),
    ],
)
def test_all_tier_edges_without_cache_discount(tokens, expected):
    usage = UsageObservation(
        completeness="complete",
        input_tokens=tokens,
        output_tokens=1,
        cache_read_input_tokens=tokens,
    )
    assert max_bounds().cost_units(usage) == amount_units(expected)


@pytest.mark.parametrize("output,expected", [(3072, "4.05504"), (4096, "4.11648")])
def test_full_input_reservation_is_top_tier_with_inherited_beta_guard(output, expected):
    bounds = max_bounds(output)
    bounds.checkpoint()
    assert isinstance(bounds, BailianBetaVerificationBounds) and bounds.model == MAX_MODEL
    assert bounds.price_source == "https://help.aliyun.com/zh/model-studio/model-qwen3-max"
    assert bounds.guard_version == "harnessix.bailian-beta-max-request-guard/v1"
    assert bounds.purpose == "beta-001-model-request" and bounds.maximum_input_tokens == 258_048
    assert bounds.price_tiers == (
        (32_000, 6_000, 24_000),
        (128_000, 10_000, 40_000),
        (262_144, 15_000, 60_000),
    )
    usage = UsageObservation(completeness="complete", input_tokens=258_048, output_tokens=output)
    assert bounds.cost_units(usage) == bounds.maximum_units == amount_units(expected)
    for name in ("purpose", "checkpoint", "maximum_units", "cost_units", "fingerprint"):
        assert name not in BailianBetaMaxVerificationBounds.__dict__


@pytest.mark.parametrize("output", [4097, 32_768])
def test_official_output_capacity_does_not_expand_host_limit(output):
    with pytest.raises(KernelError) as caught:
        max_bounds(output).checkpoint()
    assert caught.value.code == "verification_price_unavailable"


@pytest.mark.parametrize(
    "input_tokens,output_tokens,actual_model,cost",
    [
        (13, 1, MAX_MODEL, "0.000102"),
        (258_048, 3072, MAX_MODEL, "4.05504"),
        (258_049, 1, MAX_MODEL, None),
        (262_144, 1, MAX_MODEL, None),
        (13, 3073, MAX_MODEL, None),
        (13, 1, "qwen3-max", None),
        (13, 1, None, None),
        (None, None, MAX_MODEL, None),
    ],
)
async def test_beta_ten_of_sixty_settles_or_holds_unknown_and_stops(
    tmp_path, input_tokens, output_tokens, actual_model, cost
):
    path, plan = authorized_budget(tmp_path)
    before = period(path)
    usage = (
        UsageObservation()
        if input_tokens is None
        else UsageObservation(
            completeness="complete", input_tokens=input_tokens, output_tokens=output_tokens
        )
    )
    provider = MaxProvider(
        path,
        usage=usage,
        actual_model=actual_model,
        mode="missing" if input_tokens is None else "complete",
    )
    token, seen = CancelToken(), []
    with task_scope(path, plan) as ledger:
        assert ledger.allocation == amount_units("60") and plan.maximum_cost == "10"
        guard = GuardedVerificationProvider(provider, ledger, max_bounds(), token)
        if cost is None:
            with pytest.raises(KernelError) as caught:
                async for event in guard.stream(model_request(), CancelToken()):
                    seen.append(event)
            assert caught.value.code == "verification_request_cost_unknown"
            with pytest.raises(KernelError) as blocked:
                ledger.require_available()
            assert blocked.value.code == "verification_budget_unresolved"
            with pytest.raises(TurnCancelled):
                _ = [event async for event in guard.stream(model_request(), CancelToken())]
        else:
            async for event in guard.stream(model_request(), CancelToken()):
                seen.append(event)
                if isinstance(event, ResponseCompleted):
                    assert period(path)["requests"][-1]["cost_estimate"] == cost
            ledger.require_available()
    assert provider.sent == 1 and provider.closed and token.cancelled == (cost is None)
    assert provider.reservation["reserved_cost"] == "4.05504"
    assert provider.reservation["requested_model"] == MAX_MODEL
    assert provider.reservation["purpose"] == "beta-001-model-request"
    assert any(isinstance(event, ResponseCompleted) for event in seen) == (cost is not None)
    after = period(path)
    assert after["requests"][:-1] == before["requests"] and after["known_cost"] == (cost or "0")
    assert after["reserved_cost"] == ("4.05504" if cost is None else "0")
    assert after["requests"][-1]["status"] == ("unknown" if cost is None else "completed")
    if cost is None:
        saved = path.read_bytes()
        with pytest.raises(KernelError) as blocked:
            with task_scope(path, plan):
                pytest.fail("new unknown must block reopen")
        assert blocked.value.code == "verification_budget_unresolved" and path.read_bytes() == saved


@pytest.mark.parametrize(
    "task_id,suite", [(None, False), ("BETA-002", False), (None, True), ("BETA-001", True)]
)
async def test_wrong_task_or_suite_cannot_reserve(tmp_path, monkeypatch, task_id, suite):
    path, plan = authorized_budget(tmp_path)
    before = path.read_bytes()
    provider, token = MaxProvider(path, actual_model=MAX_MODEL), CancelToken()
    with task_scope(path, plan) as ledger:
        monkeypatch.setattr(ledger, "task_id", task_id)
        monkeypatch.setattr(ledger, "suite_id", uuid4() if suite else None)
        guard = GuardedVerificationProvider(provider, ledger, max_bounds(), token)
        with pytest.raises(KernelError) as caught:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert caught.value.code == "verification_budget_unresolved"
    assert provider.sent == 0 and token.cancelled and path.read_bytes() == before
