"""独立Beta 10/60元授权：空周期、封闭合同、持久累计及未知费用停机。"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.usage import UsageObservation
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts import provider_reverification_plan as plans
from scripts.provider_verification_budget import VerificationBudgetLedger
from scripts.provider_verification_guard import GuardedVerificationProvider
from tests.evals.test_beta_instruct_bounds import MockProvider, instruct_bounds
from tests.evals.test_provider_reverification_task import authorized_task, task_scope
from tests.evals.test_provider_reverification_task import isolated_io as isolated_io
from tests.evals.test_provider_reverification_v2 import write_plan
from tests.evals.test_provider_verification_budget import model_request, period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")
VERSION = "harnessix.provider-beta-task-budget/v1"


def empty_ledger(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schema": "harnessix.provider-verification-budget/v1",
                "provider": "aliyun-bailian",
                "currency": "CNY",
                "periods": [
                    {
                        "period_id": str(uuid4()),
                        "status": "active",
                        "allocation": "60",
                        "known_cost": "0",
                        "reserved_cost": "0",
                        "requests": [],
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def budget_plan(path):
    return plans.VerificationBetaTaskBudgetPlan(
        spec_version=VERSION,
        authority="budget-owner-explicit",
        reverification_id=uuid4(),
        task_id="BETA-001",
        period_id=UUID(period(path)["period_id"]),
        allocation="60",
        maximum_cost="10",
        ledger_before_sha256=sha256(path.read_bytes()).hexdigest(),
        prior_request_count=0,
        prior_requests_sha256=digest([]),
        carried_requests=(),
    )


def authorized_budget(tmp_path):
    path = empty_ledger(tmp_path)
    plan = budget_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    return path, plan


def test_empty_plan_schema_parse_private_read_and_snapshot(tmp_path):
    plan = budget_plan(empty_ledger(tmp_path))
    assert isinstance(plan, plans.VerificationBetaTaskReverificationPlan)
    schema = type(plan).model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(plan.model_dump())
    for field, value in (
        ("spec_version", VERSION),
        ("task_id", "BETA-001"),
        ("allocation", "60"),
        ("maximum_cost", "10"),
        ("prior_request_count", 0),
        ("prior_requests_sha256", digest([])),
    ):
        assert schema["properties"][field]["const"] == value
    assert schema["properties"]["carried_requests"]["maxItems"] == 0
    assert not {"suite_id", "maximum_requests"} & schema["properties"].keys()
    config = write_plan(tmp_path, plan.model_dump_json())
    for restored in (
        plans.parse_reverification_plan(plan.model_dump_json()),
        plans.read_reverification_plan(str(config)),
        plans.snapshot_reverification_plan(plan),
    ):
        assert type(restored) is plans.VerificationBetaTaskBudgetPlan
        assert restored == plan and restored is not plan
    assert plan.prior_request_count == 0 and plan.carried_requests == ()


@pytest.mark.parametrize(
    "field,value",
    [
        ("spec_version", VERSION + "/unknown"),
        ("spec_version", "harnessix.provider-task-reverification-plan/v1"),
        ("task_id", "BETA-002"),
        ("allocation", "70"),
        ("allocation", 60),
        ("maximum_cost", "5"),
        ("maximum_cost", "10.0"),
        ("maximum_cost", 10),
        ("prior_request_count", 1),
        ("prior_request_count", False),
        ("prior_request_count", 0.0),
        ("prior_requests_sha256", "0" * 64),
        ("carried_requests", [None]),
        ("suite_id", None),
        ("maximum_requests", 2),
    ],
)
def test_closed_contract_rejects_mixed_or_coerced_values(tmp_path, field, value):
    path = empty_ledger(tmp_path)
    plan = budget_plan(path)
    data = plan.model_dump(mode="json")
    data[field] = value
    text = json.dumps(data)
    config = write_plan(tmp_path, text)
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(text)
    with pytest.raises(ValueError):
        plans.read_reverification_plan(str(config))
    forged = plan.model_copy(update={field: value})
    with pytest.raises(ValueError):
        plans.snapshot_reverification_plan(forged)
    before = path.read_bytes()
    with pytest.raises(KernelError) as failed:
        VerificationBudgetLedger.authorize_reverification(path, forged)
    assert failed.value.code == "verification_reverification_invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["ledger_digest", "period_id", "nonempty"])
def test_registration_binds_exact_empty_period_and_ledger_bytes(tmp_path, change):
    path = empty_ledger(tmp_path)
    plan = budget_plan(path)
    if change == "nonempty":
        with VerificationBudgetLedger(path, plan.period_id) as ledger:
            request = ledger.reserve(amount_units("1"), {})
            ledger.settle(request, amount_units("1"), sent=True)
        plan = budget_plan(path)
    elif change == "ledger_digest":
        plan = plan.model_copy(update={"ledger_before_sha256": "0" * 64})
    else:
        plan = plan.model_copy(update={"period_id": uuid4()})
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == before


def test_registration_preserves_old_period_and_plan_and_cannot_replace_new_plan(tmp_path):
    old_dir = tmp_path / "old"
    old_dir.mkdir(mode=0o700)
    old_path, old_plan = authorized_task(old_dir)
    old_bytes = old_path.read_bytes()
    old_period = {**period(old_path), "status": "closed"}
    path = empty_ledger(tmp_path)
    data = json.loads(path.read_text())
    data["periods"].append(old_period)
    path.write_text(json.dumps(data))
    plan = budget_plan(path)
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert period(path)["requests"] == []
    registered = path.read_bytes()
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == registered
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_reverification(
            path, plan.model_copy(update={"reverification_id": uuid4()})
        )
    assert path.read_bytes() == registered
    with task_scope(path, plan) as ledger:
        request = ledger.reserve(amount_units("1"), {})
        ledger.settle(request, amount_units("1"), sent=True)
    assert json.loads(path.read_text())["periods"][1] == old_period
    assert old_period["bounded_reverification"] == old_plan.model_dump(mode="json")
    assert old_path.read_bytes() == old_bytes


def test_ten_yuan_cap_includes_reservations_and_survives_reopen(tmp_path):
    path, plan = authorized_budget(tmp_path)
    with task_scope(path, plan) as ledger:
        request = ledger.reserve(amount_units("2"), {})
        ledger.settle(request, amount_units("2"), sent=True)
    with task_scope(path, plan) as ledger:
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            ledger.reserve(amount_units("8") + 1, {})
        assert failed.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = ledger.reserve(amount_units("8"), {})
        assert plans.validate_reverification_plan(ledger.period, plan) == amount_units("10")
        assert period(path)["known_cost"] == "2" and period(path)["reserved_cost"] == "8"
        ledger.settle(request, amount_units("8"), sent=True)
    with task_scope(path, plan) as ledger:
        assert ledger.allocation == amount_units("60")
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            ledger.reserve(1, {})
        assert failed.value.code == "verification_budget_exhausted"
    assert path.read_bytes() == before
    assert period(path)["known_cost"] == "10" and period(path)["reserved_cost"] == "0"


def test_unscoped_new_period_still_enforces_sixty_yuan_total(tmp_path):
    path = empty_ledger(tmp_path)
    identity = UUID(period(path)["period_id"])
    with VerificationBudgetLedger(path, identity) as ledger:
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            ledger.reserve(amount_units("60") + 1, {})
        assert failed.value.code == "verification_budget_exhausted"
        assert path.read_bytes() == before
        request = ledger.reserve(amount_units("60"), {})
        ledger.settle(request, amount_units("60"), sent=True)
    with VerificationBudgetLedger(path, identity) as ledger:
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            ledger.reserve(1, {})
        assert failed.value.code == "verification_budget_exhausted"
    assert path.read_bytes() == before and period(path)["known_cost"] == "60"


async def test_multiple_guard_requests_have_no_continuation_or_request_count_gate(tmp_path):
    path, plan = authorized_budget(tmp_path)
    for _ in range(12):
        with task_scope(path, plan) as ledger:
            provider = MockProvider(
                path, UsageObservation(completeness="complete", input_tokens=13, output_tokens=1)
            )
            token = CancelToken()
            guard = GuardedVerificationProvider(provider, ledger, instruct_bounds(), token)
            seen = [event async for event in guard.stream(model_request(), CancelToken())]
            assert isinstance(seen[-1], ResponseCompleted)
            assert provider.calls == 1 and not token.cancelled
    current = period(path)
    assert len(current["requests"]) == 12
    assert current["known_cost"] == "0.000408" and current["reserved_cost"] == "0"
    assert all(request["task_id"] == "BETA-001" for request in current["requests"])
    assert "task_continuation" not in current and "task_continuation_chain" not in current
    assert current["bounded_reverification"] == plan.model_dump(mode="json")


@pytest.mark.parametrize("status", ["reserved", "unknown"])
def test_new_unresolved_cost_stops_owner_and_reopen_without_reset(tmp_path, status):
    path, plan = authorized_budget(tmp_path)
    with task_scope(path, plan) as ledger:
        request = ledger.reserve(amount_units("0.282624"), {})
        if status == "unknown":
            ledger.settle(request, None, sent=True)
        before = path.read_bytes()
        with pytest.raises(KernelError) as failed:
            ledger.reserve(1, {})
        assert failed.value.code == "verification_budget_unresolved"
    with pytest.raises(KernelError) as failed:
        with task_scope(path, plan):
            pytest.fail("新授权无旧费用豁免；未决请求必须阻止重开")
    assert failed.value.code == "verification_budget_unresolved"
    VerificationBudgetLedger.authorize_reverification(path, plan)
    assert path.read_bytes() == before
    assert period(path)["reserved_cost"] == "0.282624"
    assert period(path)["known_cost"] == "0"


@pytest.mark.parametrize("scope", ["none", "wrong_task", "missing_task", "wrong_grant", "suite"])
def test_new_budget_rejects_unscoped_or_cross_task_owner(tmp_path, scope):
    path, plan = authorized_budget(tmp_path)
    identities = {"task_id": "BETA-001", "reverification_id": plan.reverification_id}
    if scope == "none":
        identities = {}
    elif scope == "wrong_task":
        identities["task_id"] = "BETA-002"
    elif scope == "missing_task":
        identities.pop("task_id")
    elif scope == "wrong_grant":
        identities["reverification_id"] = uuid4()
    else:
        identities["suite_id"] = uuid4()
    before = path.read_bytes()
    with pytest.raises(KernelError) as failed:
        with VerificationBudgetLedger(path, plan.period_id, **identities):
            pytest.fail("新空周期也必须在Owner入口校验完整Beta身份")
    assert failed.value.code == "verification_budget_unresolved"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change", [{"maximum_cost": "10"}, {"prior_request_count": 0}, {"carried_requests": []}]
)
def test_original_beta_v1_keeps_five_yuan_and_nonempty_prefix(tmp_path, change):
    _, plan = authorized_task(tmp_path)
    assert type(plan) is plans.VerificationBetaTaskReverificationPlan
    assert plan.maximum_cost == "5" and plan.prior_request_count >= 1
    assert len(plan.carried_requests) == 1
    data = plan.model_dump(mode="json")
    data.update(change)
    config = write_plan(tmp_path, json.dumps(data))
    with pytest.raises(ValueError):
        plans.parse_reverification_plan(json.dumps(data))
    with pytest.raises(ValueError):
        plans.read_reverification_plan(str(config))
