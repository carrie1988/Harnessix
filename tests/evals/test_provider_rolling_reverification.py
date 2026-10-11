"""全额预留未知费用后的同一Suite恢复合同。"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts.provider_reverification_plan import (
    CarriedVerificationRequest,
    VerificationRollingReverificationPlan,
    VerificationRollingReverificationPlanV2,
    parse_reverification_plan,
)
from scripts.provider_verification_budget import VerificationBudgetLedger

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")


def _authorized_ledger(tmp_path):
    tmp_path.chmod(0o700)
    period_id, suite_id, reverification_id = uuid4(), uuid4(), uuid4()
    completed = {
        "request_id": str(uuid4()),
        "status": "completed",
        "reserved_cost": "0",
        "cost_estimate": "10",
        "purpose": "known-carryover",
    }
    unknown = {
        "request_id": str(uuid4()),
        "status": "unknown",
        "reserved_cost": "10",
        "purpose": "unknown-carryover",
    }
    requests = [completed, unknown]
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schema": "harnessix.provider-verification-budget/v1",
                "provider": "aliyun-bailian",
                "currency": "CNY",
                "periods": [
                    {
                        "period_id": str(period_id),
                        "status": "active",
                        "allocation": "60",
                        "known_cost": "10",
                        "reserved_cost": "10",
                        "requests": requests,
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    plan = VerificationRollingReverificationPlan(
        spec_version="harnessix.provider-reverification-plan/v4",
        authority="budget-owner-explicit",
        reverification_id=reverification_id,
        suite_id=suite_id,
        period_id=period_id,
        allocation="60",
        maximum_cost="40",
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        prior_request_count=len(requests),
        prior_requests_sha256=digest(requests),
        carried_requests=(
            CarriedVerificationRequest(
                request_id=UUID(unknown["request_id"]),
                reserved_cost=unknown["reserved_cost"],
                request_sha256=digest(unknown),
            ),
        ),
    )
    VerificationBudgetLedger.authorize_reverification(path, plan)
    return path, plan


def test_v4_round_trips_as_its_exact_contract(tmp_path) -> None:
    _, plan = _authorized_ledger(tmp_path)
    restored = parse_reverification_plan(plan.model_dump_json())
    assert type(restored) is VerificationRollingReverificationPlan
    assert restored == plan


def test_v4_reopens_after_new_unknown_without_refunding_its_reservation(tmp_path) -> None:
    path, plan = _authorized_ledger(tmp_path)
    scope = {
        "reverification_id": plan.reverification_id,
        "suite_id": plan.suite_id,
    }
    with VerificationBudgetLedger(path, plan.period_id, **scope) as owner:
        request_id = owner.reserve(amount_units("3"), {"purpose": "model-request"})
        owner.settle(request_id, None, sent=True)

    with VerificationBudgetLedger(path, plan.period_id, **scope) as owner:
        assert owner.period["known_cost"] == "10"
        assert owner.period["reserved_cost"] == "13"
        follow_up = owner.reserve(amount_units("2"), {"purpose": "model-request"})
        owner.settle(follow_up, 0, sent=False)

    persisted = json.loads(path.read_text(encoding="utf-8"))["periods"][0]
    assert persisted["known_cost"] == "10"
    assert persisted["reserved_cost"] == "13"
    assert [item["status"] for item in persisted["requests"][-2:]] == ["unknown", "not_sent"]


def test_v5_keeps_current_unknown_reserved_and_reopens_an_80_yuan_period(tmp_path) -> None:
    tmp_path.chmod(0o700)
    period_id, suite_id, reverification_id = uuid4(), uuid4(), uuid4()
    completed = {
        "request_id": str(uuid4()),
        "status": "completed",
        "reserved_cost": "0",
        "cost_estimate": "4.724844",
        "purpose": "known-current-period",
    }
    unknown = {
        "request_id": str(uuid4()),
        "status": "unknown",
        "reserved_cost": "3.147456",
        "purpose": "unknown-current-period",
    }
    requests = [completed, unknown]
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schema": "harnessix.provider-verification-budget/v1",
                "provider": "aliyun-bailian",
                "currency": "CNY",
                "periods": [
                    {
                        "period_id": str(period_id),
                        "status": "active",
                        "allocation": "80",
                        "known_cost": "4.724844",
                        "reserved_cost": "3.147456",
                        "requests": requests,
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    plan = VerificationRollingReverificationPlanV2(
        spec_version="harnessix.provider-reverification-plan/v5",
        authority="budget-owner-explicit",
        reverification_id=reverification_id,
        suite_id=suite_id,
        period_id=period_id,
        allocation="80",
        maximum_cost="72.1277",
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        prior_request_count=len(requests),
        prior_requests_sha256=digest(requests),
        carried_requests=(
            CarriedVerificationRequest(
                request_id=UUID(unknown["request_id"]),
                reserved_cost=unknown["reserved_cost"],
                request_sha256=digest(unknown),
            ),
        ),
    )

    VerificationBudgetLedger.authorize_reverification(path, plan)
    restored = parse_reverification_plan(plan.model_dump_json())
    assert type(restored) is VerificationRollingReverificationPlanV2

    scope = {"reverification_id": plan.reverification_id, "suite_id": plan.suite_id}
    with VerificationBudgetLedger(path, plan.period_id, **scope) as owner:
        request_id = owner.reserve(amount_units("3"), {"purpose": "model-request"})
        owner.settle(request_id, None, sent=True)

    with VerificationBudgetLedger(path, plan.period_id, **scope) as owner:
        assert owner.period["known_cost"] == "4.724844"
        assert owner.period["reserved_cost"] == "6.147456"
