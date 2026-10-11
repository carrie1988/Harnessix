"""完整 usage 对账：保留原 unknown 请求，只释放已由认证证据确定的差额。"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts.provider_task_continuation import parse_task_continuation
from scripts.provider_usage_reconciliation import (
    UsageReconciliation,
    parse_usage_reconciliation,
    usage_reconciliations,
)
from scripts.provider_verification_budget import VerificationBudgetLedger
from tests.evals.test_beta_task_budget_plan import authorized_budget
from tests.evals.test_provider_reverification_task import task_scope
from tests.evals.test_provider_task_continuation import continuation_for
from tests.evals.test_provider_task_continuation import isolated_io as isolated_io
from tests.evals.test_provider_verification_budget import period

pytestmark = pytest.mark.skipif(os.name != "posix", reason="验证宿主账本限定POSIX")

MODEL = "qwen3-max-2025-09-23"
PRICE_BASIS = "https://help.aliyun.com/zh/model-studio/model-qwen3-max"


def pending_usage(tmp_path):
    path, plan = authorized_budget(tmp_path)
    metadata = {
        "purpose": "beta-001-model-request",
        "requested_model": MODEL,
        "region": "cn-beijing",
        "max_output_tokens": 3072,
        "max_attempts": 1,
        "thread_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "step": 1,
        "price_basis": PRICE_BASIS,
    }
    with task_scope(path, plan) as owner:
        request_id = owner.reserve(amount_units("4.05504"), metadata)
        owner.settle(request_id, None, sent=True)
    raw = continuation_for(path, plan).model_dump(mode="json", exclude={"maximum_requests"})
    raw["spec_version"] = "harnessix.provider-beta-budget-continuation/v1"
    continuation = parse_task_continuation(json.dumps(raw))
    VerificationBudgetLedger.authorize_task_continuation(path, continuation)
    return path, plan, continuation, request_id


def scoped(path, plan, continuation):
    return VerificationBudgetLedger(
        path,
        plan.period_id,
        reverification_id=plan.reverification_id,
        task_id=plan.task_id,
        task_continuation_id=continuation.continuation_id,
    )


def reconciliation_for(path, request_id, **changes):
    request = next(r for r in period(path)["requests"] if r["request_id"] == str(request_id))
    values = {
        "spec_version": "harnessix.provider-usage-reconciliation/v1",
        "authority": "budget-owner-explicit",
        "reconciliation_id": uuid4(),
        "previous_reconciliation_id": None,
        "period_id": UUID(period(path)["period_id"]),
        "request_id": request_id,
        "request_sha256": digest(request),
        "ledger_before_sha256": sha256(path.read_bytes()).hexdigest(),
        "source_evidence_sha256": "a" * 64,
        "requested_model": MODEL,
        "actual_model": MODEL,
        "price_basis": PRICE_BASIS,
        "input_tokens": 100,
        "output_tokens": 10,
        "input_rate_milli_per_million": 6000,
        "output_rate_milli_per_million": 24000,
        "cost_estimate": "0.00084",
        "failure_code": "provider_invalid_provider_output",
        "diagnostic_suffix": "chat_protocol/v1:tool_name_unknown",
        "recorded_at": datetime.now(UTC),
    }
    values.update(changes)
    return UsageReconciliation(**values)


def test_append_is_idempotent_preserves_unknown_row_and_reopens_budget(tmp_path):
    path, plan, continuation, request_id = pending_usage(tmp_path)
    before_request = deepcopy(period(path)["requests"][0])
    record = reconciliation_for(path, request_id)

    VerificationBudgetLedger.reconcile_usage(path, record)
    registered = path.read_bytes()
    VerificationBudgetLedger.reconcile_usage(path, record)

    current = period(path)
    assert path.read_bytes() == registered
    assert current["requests"][0] == before_request
    assert current["known_cost"] == "0.00084"
    assert current["reserved_cost"] == "0"
    assert usage_reconciliations(current) == (record,)
    assert json.loads(registered)["schema"] == "harnessix.provider-verification-budget/v6"

    with scoped(path, plan, continuation) as owner:
        follow_up = owner.reserve(amount_units("0.01"), {})
        owner.settle(follow_up, amount_units("0.001"), sent=True)
    assert period(path)["known_cost"] == "0.00184"


def test_parser_and_snapshot_are_strict_and_frozen(tmp_path):
    path, _, _, request_id = pending_usage(tmp_path)
    record = reconciliation_for(path, request_id)
    restored = parse_usage_reconciliation(record.model_dump_json())
    assert restored == record and restored is not record
    assert type(restored.recorded_at) is datetime
    schema = UsageReconciliation.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["authority"]["const"] == "budget-owner-explicit"
    assert schema["properties"]["spec_version"]["const"] == (
        "harnessix.provider-usage-reconciliation/v1"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("ledger_before_sha256", "0" * 64),
        ("request_sha256", "0" * 64),
        ("request_id", uuid4()),
        ("actual_model", "qwen3-max"),
        ("requested_model", "qwen3-max"),
        ("price_basis", "https://example.invalid/price"),
        ("output_tokens", 3073),
        ("cost_estimate", "0.000841"),
        ("input_rate_milli_per_million", 10000),
        ("authority", "automatic"),
    ],
)
def test_mismatched_identity_usage_or_price_does_not_change_ledger(tmp_path, field, value):
    path, _, _, request_id = pending_usage(tmp_path)
    record = reconciliation_for(path, request_id).model_copy(update={field: value})
    before = path.read_bytes()
    with pytest.raises(KernelError) as failed:
        VerificationBudgetLedger.reconcile_usage(path, record)
    assert failed.value.code == "verification_usage_reconciliation_invalid"
    assert path.read_bytes() == before


def test_same_request_cannot_be_reconciled_twice_under_another_identity(tmp_path):
    path, _, _, request_id = pending_usage(tmp_path)
    first = reconciliation_for(path, request_id)
    VerificationBudgetLedger.reconcile_usage(path, first)
    second = reconciliation_for(
        path,
        request_id,
        previous_reconciliation_id=first.reconciliation_id,
    )
    before = path.read_bytes()
    with pytest.raises(KernelError):
        VerificationBudgetLedger.reconcile_usage(path, second)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["record", "aggregate", "schema", "remove"])
def test_reopen_rejects_tampered_reconciliation_or_downgraded_schema(tmp_path, mutation):
    path, plan, continuation, request_id = pending_usage(tmp_path)
    VerificationBudgetLedger.reconcile_usage(path, reconciliation_for(path, request_id))
    data = json.loads(path.read_text())
    current = data["periods"][0]
    if mutation == "record":
        current["usage_reconciliations"][0]["cost_estimate"] = "0.000841"
    elif mutation == "aggregate":
        current["reserved_cost"] = "4.05504"
    elif mutation == "schema":
        data["schema"] = "harnessix.provider-verification-budget/v4"
    else:
        current.pop("usage_reconciliations")
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(KernelError) as failed:
        with scoped(path, plan, continuation):
            pass
    assert failed.value.code == "verification_budget_unavailable"
    assert path.read_bytes() == before
