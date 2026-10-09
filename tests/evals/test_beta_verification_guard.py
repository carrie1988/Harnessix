"""Beta固定价格档与原生Adapter验证；不联网、不访问真实账本或凭据。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.usage import UsageObservation
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.pricing import amount_units
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import (
    BailianBetaVerificationBounds,
    BailianVerificationBounds,
    GuardedVerificationProvider,
)
from tests.contracts.provider import model_request as wire_request
from tests.evals.test_provider_verification_budget import (
    PERIOD,
    ObservedProvider,
    ledger_file,
    model_request,
    period,
)
from tests.models.wire import WireStream, chunk, frame, response

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


def beta_bounds(output=3072):
    now = datetime.now(UTC)
    return BailianBetaVerificationBounds(
        now - timedelta(minutes=1), now + timedelta(hours=1), output
    )


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (32_000, "0.032004"),
        (32_001, "0.0480075"),
        (128_000, "0.192006"),
        (128_001, "0.3200125"),
        (204_800, "0.51201"),
    ],
)
def test_beta_exact_price_tiers(tokens, expected):
    usage = UsageObservation(completeness="complete", input_tokens=tokens, output_tokens=1)
    assert beta_bounds().cost_units(usage) == amount_units(expected)


def test_full_model_reservation_is_independent_of_prompt_estimates():
    assert beta_bounds().maximum_units == amount_units("0.54272")
    assert beta_bounds(4096).maximum_units == amount_units("0.55296")
    current = beta_bounds()
    original = BailianVerificationBounds(current.valid_from, current.valid_until, 4096)
    assert original.maximum_units == amount_units("20.77824")
    assert original.model == "qwen3-coder-plus-2025-09-23"


@pytest.mark.parametrize("input_tokens,output_tokens", [(204801, 1), (10, 3073)])
def test_out_of_contract_usage_cannot_release_reserved_money(input_tokens, output_tokens):
    usage = UsageObservation(
        completeness="complete", input_tokens=input_tokens, output_tokens=output_tokens
    )
    assert beta_bounds().cost_units(usage) is None


async def test_suite_or_unscoped_owner_cannot_use_beta_model(tmp_path):
    path = ledger_file(tmp_path)
    before = path.read_bytes()
    with VerificationBudgetLedger(path, PERIOD) as ledger:
        provider = ObservedProvider(path)
        token = CancelToken()
        guard = GuardedVerificationProvider(provider, ledger, beta_bounds(), token)
        with pytest.raises(KernelError, match="Beta模型只属于单任务授权"):
            _ = [event async for event in guard.stream(model_request(), CancelToken())]
        assert provider.sent == 0 and token.cancelled
    assert path.read_bytes() == before


def authorized_beta_ledger(tmp_path):
    # 延用真实Ledger的独占、旧unknown承接与task登记，不用假的预算对象替代。
    from scripts.provider_reverification_plan import parse_reverification_plan
    from tests.evals.test_provider_reverification import plan_for
    from tests.evals.test_provider_reverification_v2 import held_ledger

    path = held_ledger(tmp_path)
    value = plan_for(path).model_dump(mode="json")
    value.pop("suite_id")
    value.update(
        spec_version="harnessix.provider-task-reverification-plan/v1",
        task_id="BETA-001",
        allocation="60",
        maximum_cost="5",
    )
    plan = parse_reverification_plan(json.dumps(value))
    VerificationBudgetLedger.authorize_reverification(path, plan)
    return path, plan


@pytest.mark.parametrize("case", ["complete", "missing_usage", "wrong_model", "truncated"])
async def test_beta_native_adapter_preserves_reserve_settle_and_unknown_stop(tmp_path, case):
    path, plan = authorized_beta_ledger(tmp_path)
    bounds = beta_bounds(128)
    parts = [chunk({"content": "fixture"}), chunk(finish="stop"), chunk(usage=True)]
    for part in parts:
        part["model"] = "wrong-model" if case == "wrong_model" else bounds.model
    if case == "missing_usage":
        parts.pop()
    wire = WireStream(
        [frame(part) for part in parts] + ([] if case == "truncated" else [b"data: [DONE]\n\n"])
    )
    sent = 0

    def transport(request):
        nonlocal sent
        assert period(path)["requests"][-1]["status"] == "reserved"
        payload = json.loads(request.content)
        assert payload["model"] == bounds.model and payload["max_tokens"] == 128
        sent += 1
        return response(wire)

    token = CancelToken()
    seen = []
    with VerificationBudgetLedger(
        path, PERIOD, reverification_id=plan.reverification_id, task_id="BETA-001"
    ) as ledger:
        async with OpenAIChatProvider(
            OpenAIChatConfig(
                model=bounds.model,
                max_output_tokens=128,
                max_attempts=1,
                retry_delay_seconds=0,
                output_token_parameter="max_tokens",
            ),
            api_key="fixture-unusable",
            transport=httpx.MockTransport(transport),
        ) as provider:
            guard = GuardedVerificationProvider(provider, ledger, bounds, token)
            if case == "complete":
                seen = [e async for e in guard.stream(wire_request(), CancelToken())]
            else:
                try:
                    seen = [e async for e in guard.stream(wire_request(), CancelToken())]
                except KernelError:
                    pass
                with pytest.raises(KernelError):
                    ledger.require_available()
    latest = period(path)["requests"][-1]
    assert sent == 1 and wire.closed
    assert latest["requested_model"] == bounds.model and latest["task_id"] == "BETA-001"
    assert latest["purpose"] == "beta-001-model-request"
    assert latest["status"] == ("completed" if case == "complete" else "unknown")
    assert any(isinstance(e, ResponseCompleted) for e in seen) == (case == "complete")
    assert token.cancelled == (case != "complete")
