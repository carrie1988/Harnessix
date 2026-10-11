"""当前50元R3复验合同；保留旧未决并约束新Suite累计费用。"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts.provider_reverification_plan import (
    CarriedVerificationRequest,
    VerificationReverificationPlanV3,
    parse_reverification_plan,
)
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import GuardedVerificationProvider
from tests.evals.test_provider_verification_budget import (
    PERIOD,
    ObservedProvider,
    bounds,
    consume,
    ledger_file,
    period,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


def current_ledger(tmp_path):
    path = ledger_file(tmp_path, allocation="50", known="0.074793")
    with VerificationBudgetLedger(path, PERIOD) as owner:
        request_id = owner.reserve(amount_units("5.09216"), {"purpose": "old-unknown"})
        owner.settle(request_id, None, sent=True)
    return path


def current_plan(path):
    current = period(path)
    held = current["requests"][-1]
    return VerificationReverificationPlanV3(
        spec_version="harnessix.provider-reverification-plan/v3",
        authority="budget-owner-explicit",
        reverification_id=uuid4(),
        suite_id=uuid4(),
        period_id=PERIOD,
        allocation="50",
        maximum_cost="40",
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        prior_request_count=len(current["requests"]),
        prior_requests_sha256=digest(current["requests"]),
        carried_requests=(
            CarriedVerificationRequest(
                request_id=UUID(held["request_id"]),
                reserved_cost=held["reserved_cost"],
                request_sha256=digest(held),
            ),
        ),
    )


def scoped(path, plan):
    return VerificationBudgetLedger(
        path,
        PERIOD,
        reverification_id=plan.reverification_id,
        suite_id=plan.suite_id,
    )


def test_v3_contract_is_exact_and_rejects_budget_drift(tmp_path):
    path = current_ledger(tmp_path)
    plan = current_plan(path)
    parsed = parse_reverification_plan(plan.model_dump_json())
    assert type(parsed) is VerificationReverificationPlanV3
    assert parsed.allocation == "50" and parsed.maximum_cost == "40"

    payload = plan.model_dump(mode="json")
    payload["maximum_cost"] = "41"
    with pytest.raises(ValueError):
        parse_reverification_plan(json.dumps(payload))


async def test_v3_preserves_prefix_and_settles_only_new_request(tmp_path):
    path = current_ledger(tmp_path)
    original = period(path)
    plan = current_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)

    token = CancelToken()
    with scoped(path, plan) as owner:
        provider = ObservedProvider(path)
        events = await consume(GuardedVerificationProvider(provider, owner, bounds(), token))

    current = period(path)
    assert isinstance(events[-1], ResponseCompleted)
    assert provider.sent == 1 and provider.closed and not token.cancelled
    assert current["requests"][: plan.prior_request_count] == original["requests"]
    assert current["known_cost"] == "0.07481"
    assert current["reserved_cost"] == "5.09216"
    assert current["requests"][-1]["reverification_id"] == str(plan.reverification_id)


async def test_v3_stops_after_any_new_unknown(tmp_path):
    path = current_ledger(tmp_path)
    plan = current_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    token = CancelToken()

    with scoped(path, plan) as owner:
        provider = ObservedProvider(path, "missing")
        with pytest.raises(KernelError) as failed:
            await consume(GuardedVerificationProvider(provider, owner, bounds(), token))
        assert failed.value.code == "verification_request_cost_unknown"
        with pytest.raises(KernelError) as stopped:
            owner.reserve(1, {})
        assert stopped.value.code == "verification_budget_unresolved"

    assert token.cancelled and provider.sent == 1 and provider.closed
    assert period(path)["requests"][-1]["status"] == "unknown"
