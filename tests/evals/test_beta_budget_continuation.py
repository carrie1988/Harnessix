"""完整Beta预算的显式未决承接；不增加额度、不改历史、不借旧次数合同扩权。"""

import json
from hashlib import sha256
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.models.pricing import amount_units
from scripts.provider_task_continuation import (
    VerificationBetaBudgetContinuation,
    parse_task_continuation,
    snapshot_task_continuation,
)
from scripts.provider_verification_budget import VerificationBudgetLedger
from tests.evals.test_beta_task_budget_plan import authorized_budget
from tests.evals.test_provider_reverification_task import task_scope
from tests.evals.test_provider_task_continuation import continuation_for, pending_beta
from tests.evals.test_provider_task_continuation import isolated_io as isolated_io
from tests.evals.test_provider_verification_budget import period


def pending_budget(tmp_path):
    path, plan = authorized_budget(tmp_path)
    with task_scope(path, plan) as owner:
        known = owner.reserve(amount_units("1.341082"), {})
        owner.settle(known, amount_units("1.341082"), sent=True)
        unknown = owner.reserve(amount_units("4.05504"), {})
        owner.settle(unknown, None, sent=True)
    data = continuation_for(path, plan).model_dump(mode="json", exclude={"maximum_requests"})
    data["spec_version"] = "harnessix.provider-beta-budget-continuation/v1"
    record = parse_task_continuation(json.dumps(data))
    return path, plan, record


def scoped(path, plan, record):
    return VerificationBudgetLedger(
        path,
        plan.period_id,
        reverification_id=plan.reverification_id,
        task_id=plan.task_id,
        task_continuation_id=record.continuation_id,
    )


def test_append_retains_all_cost_and_history_without_one_or_four_request_gate(tmp_path):
    path, plan, record = pending_budget(tmp_path)
    before = period(path)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    registered = path.read_bytes()
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    assert path.read_bytes() == registered
    assert type(record) is VerificationBetaBudgetContinuation and record.maximum_requests is None
    assert "maximum_requests" not in record.model_dump()
    with pytest.raises(KernelError, match="身份不匹配"):
        with task_scope(path, plan):
            pass
    with scoped(path, plan, record) as ledger:
        for _ in range(6):
            request = ledger.reserve(amount_units("0.01"), {})
            ledger.settle(request, amount_units("0.01"), sent=True)
        ledger.require_available()
        with pytest.raises(KernelError) as caught:
            ledger.reserve(amount_units("4.603878"), {})
        assert caught.value.code == "verification_budget_exhausted"
    after = period(path)
    assert after["bounded_reverification"] == before["bounded_reverification"]
    assert after["requests"][: len(before["requests"])] == before["requests"]
    assert after["known_cost"] == "1.401082" and after["reserved_cost"] == "4.05504"
    assert after["allocation"] == "60"
    assert all(
        r["task_continuation_id"] == str(record.continuation_id) for r in after["requests"][2:]
    )


def test_new_unknown_stops_without_reclassifying_or_releasing_any_reserve(tmp_path):
    path, plan, record = pending_budget(tmp_path)
    VerificationBudgetLedger.authorize_task_continuation(path, record)
    with scoped(path, plan, record) as ledger:
        request = ledger.reserve(amount_units("4.05504"), {})
        ledger.settle(request, None, sent=True)
        with pytest.raises(KernelError):
            ledger.require_available()
    before = path.read_bytes()
    with pytest.raises(KernelError):
        with scoped(path, plan, record):
            pass
    assert path.read_bytes() == before and period(path)["reserved_cost"] == "8.11008"
    assert period(path)["known_cost"] == "1.341082"


@pytest.mark.parametrize(
    "field,value",
    [
        ("maximum_requests", None),
        ("maximum_requests", 1),
        ("maximum_cost", "60"),
        ("previous_continuation_id", str(uuid4())),
        ("prior_request_count", True),
        ("authority", "automatic"),
        ("spec_version", []),
        ("spec_version", {}),
    ],
)
def test_closed_shape_rejects_implicit_new_permissions(tmp_path, field, value):
    path, _, record = pending_budget(tmp_path)
    before = path.read_bytes()
    raw = {**record.model_dump(mode="json"), field: value}
    with pytest.raises(ValueError):
        parse_task_continuation(json.dumps(raw))
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["prefix", "amount", "record_hash", "owner_hash", "replace"])
def test_changed_history_or_unapproved_replacement_refused(tmp_path, mutation):
    path, _, record = pending_budget(tmp_path)
    raw = record.model_dump(mode="json")
    if mutation == "prefix":
        raw["prior_requests_sha256"] = "0" * 64
    elif mutation == "owner_hash":
        raw["ledger_before_sha256"] = "0" * 64
    elif mutation == "amount":
        raw["carried_requests"][0]["reserved_cost"] = "0.01"
    elif mutation == "record_hash":
        raw["carried_requests"][0]["request_sha256"] = "0" * 64
    else:
        VerificationBudgetLedger.authorize_task_continuation(path, record)
        raw["continuation_id"] = str(uuid4())
        raw["ledger_before_sha256"] = sha256(path.read_bytes()).hexdigest()
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(
            path, parse_task_continuation(json.dumps(raw))
        )
    assert path.read_bytes() == before


def test_old_five_yuan_plan_cannot_receive_uncapped_budget_continuation(tmp_path):
    path, _, old = pending_beta(tmp_path, register=False)
    raw = old.model_dump(mode="json", exclude={"maximum_requests"})
    raw["spec_version"] = "harnessix.provider-beta-budget-continuation/v1"
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(
            path, parse_task_continuation(json.dumps(raw))
        )
    assert path.read_bytes() == before


def test_legacy_single_request_record_cannot_change_full_budget_contract(tmp_path):
    path, plan, record = pending_budget(tmp_path)
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.authorize_task_continuation(path, continuation_for(path, plan))
    forged = record.model_copy(update={"prior_request_count": True})
    with pytest.raises(ValueError):
        snapshot_task_continuation(forged)
    assert path.read_bytes() == before
