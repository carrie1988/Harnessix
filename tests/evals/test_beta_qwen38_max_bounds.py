"""Beta Qwen3.8 Max 预算边界；仅使用内存 Provider 与临时账本。"""

from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.usage import ModelAttemptStarted, UsageObservation
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from scripts.provider_verification_guard import (
    BailianBetaQwen38MaxVerificationBounds,
    GuardedVerificationProvider,
)
from tests.evals.test_beta_coder_flash_bounds import FlashProvider
from tests.evals.test_beta_coder_flash_bounds import pytestmark as pytestmark
from tests.evals.test_beta_task_budget_plan import authorized_budget
from tests.evals.test_provider_reverification_task import isolated_io as isolated_io
from tests.evals.test_provider_reverification_task import task_scope
from tests.evals.test_provider_verification_budget import model_request, period

MODEL = "qwen3.8-max"


def qwen38_bounds(output: int = 3072) -> BailianBetaQwen38MaxVerificationBounds:
    now = datetime.now(UTC)
    return BailianBetaQwen38MaxVerificationBounds(
        now - timedelta(minutes=1), now + timedelta(hours=1), output
    )


class Qwen38MaxProvider(FlashProvider):
    async def stream(self, request, cancel):
        async with aclosing(super().stream(request, cancel)) as events:
            async for event in events:
                if isinstance(event, ModelAttemptStarted):
                    event = event.model_copy(update={"requested_model": MODEL})
                yield event


def test_qwen38_max_reservation_binds_local_request_limit_and_official_price():
    bounds = qwen38_bounds()

    bounds.checkpoint()

    assert bounds.model == MODEL
    assert bounds.price_source == "https://help.aliyun.com/zh/model-studio/qwen3-8-max"
    assert bounds.guard_version == "harnessix.bailian-beta-qwen38-max-request-guard/v1"
    assert bounds.maximum_input_tokens == 250_000
    assert bounds.maximum_request_bytes == 250_000
    assert bounds.price_tiers == ((1_000_000, 12_000, 36_000),)
    assert bounds.maximum_units == amount_units("3.110592")


def test_qwen38_max_request_limit_is_part_of_guard_fingerprint(tmp_path):
    class WiderRequest(BailianBetaQwen38MaxVerificationBounds):
        maximum_request_bytes = 300_000

    path, plan = authorized_budget(tmp_path)
    bounds = qwen38_bounds()
    wider = WiderRequest(bounds.valid_from, bounds.valid_until, bounds.max_output_tokens)

    with task_scope(path, plan) as ledger:
        assert bounds.fingerprint(ledger) != wider.fingerprint(ledger)


@pytest.mark.parametrize(
    "input_tokens,output_tokens,expected",
    [
        (0, 0, "0"),
        (13, 1, "0.000192"),
        (250_000, 3072, "3.110592"),
    ],
)
def test_qwen38_max_settles_exact_uncached_price(input_tokens, output_tokens, expected):
    usage = UsageObservation(
        completeness="complete",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=input_tokens,
    )

    assert qwen38_bounds().cost_units(usage) == amount_units(expected)


async def test_qwen38_max_reserves_before_send_and_settles_complete_usage(tmp_path):
    path, plan = authorized_budget(tmp_path)
    before = period(path)
    usage = UsageObservation(completeness="complete", input_tokens=13, output_tokens=1)
    provider = Qwen38MaxProvider(path, usage=usage, actual_model=MODEL)
    suite_cancel = CancelToken()

    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(
            provider, ledger, qwen38_bounds(), suite_cancel
        )
        seen = [event async for event in guard.stream(model_request(), CancelToken())]
        ledger.require_available()

    assert isinstance(seen[-1], ResponseCompleted)
    assert provider.sent == 1 and provider.closed and not suite_cancel.cancelled
    assert provider.reservation["reserved_cost"] == "3.110592"
    assert provider.reservation["requested_model"] == MODEL
    after = period(path)
    assert after["requests"][:-1] == before["requests"]
    assert after["known_cost"] == "0.000192" and after["reserved_cost"] == "0"


@pytest.mark.parametrize(
    "usage,actual_model",
    [
        (UsageObservation(completeness="complete", input_tokens=250_001, output_tokens=1), MODEL),
        (UsageObservation(completeness="complete", input_tokens=1, output_tokens=3073), MODEL),
        (UsageObservation(), MODEL),
        (UsageObservation(completeness="complete", input_tokens=1, output_tokens=1), None),
    ],
)
async def test_qwen38_max_unknown_cost_holds_full_reservation_and_stops(
    tmp_path, usage, actual_model
):
    path, plan = authorized_budget(tmp_path)
    provider = Qwen38MaxProvider(path, usage=usage, actual_model=actual_model)
    suite_cancel = CancelToken()

    with task_scope(path, plan) as ledger:
        guard = GuardedVerificationProvider(
            provider, ledger, qwen38_bounds(), suite_cancel
        )
        with pytest.raises(KernelError) as failed:
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert failed.value.code == "verification_request_cost_unknown"
        with pytest.raises(KernelError) as blocked:
            ledger.require_available()
        assert blocked.value.code == "verification_budget_unresolved"
        with pytest.raises(TurnCancelled):
            _ = [event async for event in guard.stream(model_request(), CancelToken())]

    latest = period(path)["requests"][-1]
    assert provider.sent == 1 and provider.closed and suite_cancel.cancelled
    assert latest["status"] == "unknown" and latest["reserved_cost"] == "3.110592"
