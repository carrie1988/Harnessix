"""同一复验授权的追加候选链；保留历史绑定、全部请求和原累计费用上限。"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationInfo, model_validator

from harnessix.domain.models import ContractModel
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts.provider_reverification_binding import (
    VerificationReverificationBinding,
    validate_reverification_binding,
)
from scripts.provider_reverification_plan import (
    Amount,
    Sha256,
    VerificationBetaTaskReverificationPlan,
    VerificationReverificationPlanRecord,
    validate_reverification_plan,
)

MAX_CANDIDATE_BINDINGS = 32
CHAIN_SCHEMA = "harnessix.provider-verification-budget/v3"


class VerificationCandidateBinding(ContractModel):
    """仅追加新的活动Suite；前驱摘要与请求前缀防止覆盖旧候选或重新授予额度。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    spec_version: Literal["harnessix.provider-reverification-binding/v2"]
    authority: Literal["budget-owner-explicit"]
    binding_id: UUID
    reverification_id: UUID
    period_id: UUID
    previous_suite_id: UUID
    suite_id: UUID
    previous_binding_sha256: Sha256
    sequence: Annotated[int, Field(ge=1, le=MAX_CANDIDATE_BINDINGS)]
    ledger_before_sha256: Sha256
    original_plan_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    charged_cost: Amount
    remaining_cost: Amount

    @model_validator(mode="before")
    @classmethod
    def original_types(cls, value: object, info: ValidationInfo) -> object:
        """Python管理输入先查实际类型，不能通过序列化规范化伪模型字段。"""
        if info.mode == "python":
            if type(value) is not dict or set(value) != set(cls.model_fields):
                raise ValueError
            for name in cls.model_fields:
                expected = (
                    UUID
                    if name.endswith("_id")
                    else int
                    if name in {"sequence", "prior_request_count"}
                    else str
                )
                if type(value[name]) is not expected:
                    raise ValueError
        return value


def snapshot_candidate_binding(value: object) -> VerificationCandidateBinding:
    """重建严格不可变管理记录，不消费未经校验的copy/construct或扩展字段。"""
    if (
        type(value) is not VerificationCandidateBinding
        or set(value.__dict__) != set(VerificationCandidateBinding.model_fields)
        or value.__pydantic_extra__ is not None
    ):
        raise ValueError
    return VerificationCandidateBinding.model_validate(dict(value.__dict__))


def candidate_bindings(period: dict[str, Any]) -> tuple[VerificationCandidateBinding, ...]:
    """严格读取全部追加记录，不返回可变账本列表或猜测缺失的活动候选。"""
    raw = period.get("reverification_binding_chain", [])
    if type(raw) is not list or len(raw) > MAX_CANDIDATE_BINDINGS:
        raise ValueError
    return tuple(
        VerificationCandidateBinding.model_validate_json(json.dumps(item), strict=True)
        for item in raw
    )


def validate_candidate_chain(
    period: dict[str, Any],
    plan: VerificationReverificationPlanRecord,
    original: VerificationReverificationBinding,
    bindings: tuple[VerificationCandidateBinding, ...],
) -> None:
    """分段核验旧V1与每个V2候选，同时按原授权累计全部费用，新增未决不豁免。"""
    if (
        isinstance(plan, VerificationBetaTaskReverificationPlan)
        or not bindings
        or len(bindings) > MAX_CANDIDATE_BINDINGS
    ):
        raise ValueError
    requests = period["requests"]
    validate_reverification_binding(
        {**period, "requests": requests[: bindings[0].prior_request_count]}, plan, original
    )
    previous: VerificationReverificationBinding | VerificationCandidateBinding = original
    seen_ids = {original.binding_id}
    seen_suites = {plan.suite_id, original.suite_id}
    carried = {str(item.request_id) for item in plan.carried_requests}
    for position, binding in enumerate(bindings, start=1):
        prefix = requests[: binding.prior_request_count]
        if (
            binding.sequence != position
            or binding.period_id != plan.period_id
            or binding.reverification_id != plan.reverification_id
            or binding.previous_suite_id != previous.suite_id
            or binding.previous_binding_sha256 != digest(previous.model_dump(mode="json"))
            or binding.binding_id in seen_ids
            or binding.suite_id in seen_suites
            or binding.original_plan_sha256 != digest(plan.model_dump(mode="json"))
            or binding.prior_request_count < previous.prior_request_count
            or len(prefix) != binding.prior_request_count
            or digest(prefix) != binding.prior_requests_sha256
            or any(item["status"] == "reserved" for item in prefix)
            or {item["request_id"] for item in prefix if item["status"] == "unknown"} != carried
        ):
            raise ValueError
        charged = validate_reverification_plan({**period, "requests": prefix}, plan)
        if (
            amount_units(binding.remaining_cost) <= 0
            or charged != amount_units(binding.charged_cost)
            or charged + amount_units(binding.remaining_cost) != amount_units(plan.maximum_cost)
        ):
            raise ValueError
        end = bindings[position].prior_request_count if position < len(bindings) else len(requests)
        for request in requests[binding.prior_request_count : end]:
            if request.get("reverification_binding_id") != str(binding.binding_id) or request.get(
                "suite_id"
            ) != str(binding.suite_id):
                raise ValueError
        seen_ids.add(binding.binding_id)
        seen_suites.add(binding.suite_id)
        previous = binding
