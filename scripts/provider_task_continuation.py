"""原Beta授权内的追加式承接；冻结全部历史未决，不替换原计划或重开额度。"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from harnessix.domain.models import ContractModel
from harnessix.models._json import strict_json
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts.provider_reverification_plan import (
    CarriedVerificationRequest,
    Sha256,
    VerificationBetaTaskBudgetPlan,
    VerificationBetaTaskReverificationPlan,
    VerificationReverificationPlanRecord,
    validate_reverification_plan,
)
from scripts.provider_usage_reconciliation import reconciliation_costs

TASK_CONTINUATION_SCHEMA = "harnessix.provider-verification-budget/v4"
TASK_CONTINUATION_CHAIN_SCHEMA = "harnessix.provider-verification-budget/v5"
MAX_TASK_CONTINUATION_BYTES = 512 * 1024
MAX_TASK_CONTINUATIONS = 32


class _TaskContinuationBase(ContractModel):
    """复用原任务累计金额；每次明确授权冻结截至登记时的完整请求历史。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    authority: Literal["budget-owner-explicit"]
    continuation_id: UUID
    reverification_id: UUID
    period_id: UUID
    task_id: Literal["BETA-001"]
    ledger_before_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    carried_requests: Annotated[
        tuple[CarriedVerificationRequest, ...], Field(min_length=1, max_length=10000)
    ]

    @field_validator("maximum_requests", mode="before", check_fields=False)
    @classmethod
    def exact_request_limit(cls, value: Any) -> Any:
        # Literal[1]自身可能接受True/1.0；授权次数不能经过此类隐式规范化。
        if type(value) is not int:
            raise ValueError
        return value


class VerificationTaskContinuation(_TaskContinuationBase):
    """原单请求许可保持不变，不能通过升级扩大其次数。"""

    spec_version: Literal["harnessix.provider-task-continuation/v1"]
    maximum_requests: Literal[1]


class VerificationTaskContinuationV2(_TaskContinuationBase):
    """用户另授的最多四次许可；只追加，明确撤销前驱继续使用的资格。"""

    spec_version: Literal["harnessix.provider-task-continuation/v2"]
    maximum_requests: Literal[4]
    previous_continuation_id: UUID


class VerificationBetaBudgetContinuation(_TaskContinuationBase):
    """原60/10累计授权内保留已冻结未决；不新增次数或金额授权。"""

    spec_version: Literal["harnessix.provider-beta-budget-continuation/v1"]

    @property
    def maximum_requests(self) -> None:
        return None


type TaskContinuationRecord = (
    VerificationTaskContinuation
    | VerificationTaskContinuationV2
    | VerificationBetaBudgetContinuation
)


def parse_task_continuation(text: str) -> TaskContinuationRecord:
    """封闭版本、重复键及字节上限均在模型进入账本之前检查。"""
    if len(text.encode("utf-8")) > MAX_TASK_CONTINUATION_BYTES:
        raise ValueError
    raw = strict_json(text)
    version = raw.get("spec_version") if isinstance(raw, dict) else None
    if version == "harnessix.provider-beta-budget-continuation/v1":
        return VerificationBetaBudgetContinuation.model_validate_json(text, strict=True)
    if version == "harnessix.provider-task-continuation/v2":
        return VerificationTaskContinuationV2.model_validate_json(text, strict=True)
    return VerificationTaskContinuation.model_validate_json(text, strict=True)


def snapshot_task_continuation(value: object) -> TaskContinuationRecord:
    """不信任伪类或construct/copy绕过；递归重建严格冻结的合同。"""
    if (
        type(value)
        not in (
            VerificationTaskContinuation,
            VerificationTaskContinuationV2,
            VerificationBetaBudgetContinuation,
        )
        or set(value.__dict__) != set(type(value).model_fields)
        or value.__pydantic_extra__ is not None
        or type(value.carried_requests) is not tuple
    ):
        raise ValueError
    carried = []
    for request in value.carried_requests:
        if (
            type(request) is not CarriedVerificationRequest
            or set(request.__dict__) != set(CarriedVerificationRequest.model_fields)
            or request.__pydantic_extra__ is not None
        ):
            raise ValueError
        carried.append(
            CarriedVerificationRequest.model_validate(dict(request.__dict__), strict=True)
        )
    checked = type(value).model_validate(
        {**value.__dict__, "carried_requests": tuple(carried)}, strict=True
    )
    return parse_task_continuation(checked.model_dump_json())


def validate_task_continuation(
    period: dict[str, Any],
    plan: VerificationReverificationPlanRecord,
    record: TaskContinuationRecord,
) -> None:
    """原计划计费不变；V2必须有完整追加链，不能孤立成为新许可。"""
    record = snapshot_task_continuation(record)
    if type(record) is VerificationTaskContinuationV2:
        records = task_continuation_chain(period)
        if not records or records[-1] != record:
            raise ValueError
        validate_task_continuation_chain(period, plan, records)
    else:
        _validate_window(period, plan, record)


def _validate_window(
    period: dict[str, Any],
    plan: VerificationReverificationPlanRecord,
    record: TaskContinuationRecord,
) -> None:
    budget_only = type(record) is VerificationBetaBudgetContinuation
    expected_plan = (
        VerificationBetaTaskBudgetPlan if budget_only else VerificationBetaTaskReverificationPlan
    )
    if type(plan) is not expected_plan:
        raise ValueError
    validate_reverification_plan(period, plan)
    requests = period["requests"]
    prefix = requests[: record.prior_request_count]
    suffix = requests[record.prior_request_count :]
    reconciled = reconciliation_costs(period)
    if (
        record.period_id != plan.period_id
        or record.reverification_id != plan.reverification_id
        or record.task_id != plan.task_id
        or record.prior_request_count < plan.prior_request_count
        or len(prefix) != record.prior_request_count
        or digest(prefix) != record.prior_requests_sha256
        or any(r["status"] == "reserved" for r in prefix)
        or (
            type(record) in (VerificationTaskContinuation, VerificationBetaBudgetContinuation)
            and any("task_continuation_id" in r for r in prefix)
        )
        or (record.maximum_requests is not None and len(suffix) > record.maximum_requests)
        or any(
            r["status"] not in {"completed", "not_sent"} and r["request_id"] not in reconciled
            for r in suffix[:-1]
        )
    ):
        raise ValueError
    unknown = {r["request_id"]: r for r in prefix if r["status"] == "unknown"}
    carried = {str(r.request_id): r for r in record.carried_requests}
    if len(carried) != len(record.carried_requests) or set(unknown) != set(carried):
        raise ValueError
    for identity, request in unknown.items():
        reference = carried[identity]
        if (
            reference.reserved_cost != request["reserved_cost"]
            or amount_units(reference.reserved_cost) <= 0
            or reference.request_sha256 != digest(request)
        ):
            raise ValueError
    if any(r.get("task_continuation_id") != str(record.continuation_id) for r in suffix):
        raise ValueError


def task_continuation_chain(period: dict[str, Any]) -> tuple[VerificationTaskContinuationV2, ...]:
    raw = period.get("task_continuation_chain", [])
    if type(raw) is not list or len(raw) > MAX_TASK_CONTINUATIONS:
        raise ValueError
    records = tuple(parse_task_continuation(json.dumps(item)) for item in raw)
    if any(type(item) is not VerificationTaskContinuationV2 for item in records):
        raise ValueError
    return records


def validate_task_continuation_chain(
    period: dict[str, Any],
    plan: VerificationReverificationPlanRecord,
    records: tuple[VerificationTaskContinuationV2, ...],
) -> None:
    """按各次授权分段核验，旧记录/费用保留，最新段之外不能新增或结算请求。"""
    original = parse_task_continuation(json.dumps(period["task_continuation"]))
    if type(original) is not VerificationTaskContinuation or not records:
        raise ValueError
    previous = original
    seen = {original.continuation_id}
    requests = period["requests"]
    for record in records:
        if (
            record.previous_continuation_id != previous.continuation_id
            or record.continuation_id in seen
            or record.prior_request_count <= previous.prior_request_count
        ):
            raise ValueError
        _validate_window(
            {**period, "requests": requests[: record.prior_request_count]}, plan, previous
        )
        seen.add(record.continuation_id)
        previous = record
    _validate_window(period, plan, previous)
