"""原Beta授权内的一次承接；冻结全部历史未决，不替换原计划或重开额度。"""

from __future__ import annotations

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
    VerificationBetaTaskReverificationPlan,
    VerificationReverificationPlanRecord,
    validate_reverification_plan,
)

TASK_CONTINUATION_SCHEMA = "harnessix.provider-verification-budget/v4"
MAX_TASK_CONTINUATION_BYTES = 512 * 1024


class VerificationTaskContinuation(ContractModel):
    """预算所有者的唯一单请求许可；不包含可重新授予的金额字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    spec_version: Literal["harnessix.provider-task-continuation/v1"]
    authority: Literal["budget-owner-explicit"]
    continuation_id: UUID
    reverification_id: UUID
    period_id: UUID
    task_id: Literal["BETA-001"]
    maximum_requests: Literal[1]
    ledger_before_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    carried_requests: Annotated[
        tuple[CarriedVerificationRequest, ...], Field(min_length=1, max_length=10000)
    ]

    @field_validator("maximum_requests", mode="before")
    @classmethod
    def exact_request_limit(cls, value: Any) -> Any:
        # Literal[1]自身可能接受True/1.0；授权次数不能经过此类隐式规范化。
        if type(value) is not int:
            raise ValueError
        return value


def parse_task_continuation(text: str) -> VerificationTaskContinuation:
    """封闭版本、重复键及字节上限均在模型进入账本之前检查。"""
    if len(text.encode("utf-8")) > MAX_TASK_CONTINUATION_BYTES:
        raise ValueError
    strict_json(text)
    return VerificationTaskContinuation.model_validate_json(text, strict=True)


def snapshot_task_continuation(value: object) -> VerificationTaskContinuation:
    """不信任伪类或construct/copy绕过；递归重建严格冻结的合同。"""
    if (
        type(value) is not VerificationTaskContinuation
        or set(value.__dict__) != set(VerificationTaskContinuation.model_fields)
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
    checked = VerificationTaskContinuation.model_validate(
        {**value.__dict__, "carried_requests": tuple(carried)}, strict=True
    )
    return parse_task_continuation(checked.model_dump_json())


def validate_task_continuation(
    period: dict[str, Any],
    plan: VerificationReverificationPlanRecord,
    record: VerificationTaskContinuation,
) -> None:
    """原计划计费不变；承接完整旧unknown前缀，后缀只允许零或一笔同身份请求。"""
    record = snapshot_task_continuation(record)
    if type(plan) is not VerificationBetaTaskReverificationPlan:
        raise ValueError
    validate_reverification_plan(period, plan)
    requests = period["requests"]
    prefix = requests[: record.prior_request_count]
    suffix = requests[record.prior_request_count :]
    if (
        record.period_id != plan.period_id
        or record.reverification_id != plan.reverification_id
        or record.task_id != plan.task_id
        or record.prior_request_count < plan.prior_request_count
        or len(prefix) != record.prior_request_count
        or digest(prefix) != record.prior_requests_sha256
        or any(r["status"] == "reserved" or "task_continuation_id" in r for r in prefix)
        or len(suffix) > record.maximum_requests
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
