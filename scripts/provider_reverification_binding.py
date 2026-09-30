"""同一复验额度的单次Suite切换合同；冻结原授权及切换前全部请求。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from harnessix.domain.models import ContractModel
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest
from scripts.provider_reverification_plan import (
    Amount,
    Sha256,
    VerificationReverificationPlan,
    validate_reverification_plan,
)


class VerificationReverificationBinding(ContractModel):
    """明确切换到唯一新Suite；已用费用承接，不生成新的40元额度。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    spec_version: Literal["harnessix.provider-reverification-binding/v1"]
    authority: Literal["budget-owner-explicit"]
    binding_id: UUID
    reverification_id: UUID
    period_id: UUID
    previous_suite_id: UUID
    suite_id: UUID
    ledger_before_sha256: Sha256
    original_plan_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    charged_cost: Amount
    remaining_cost: Amount


def validate_reverification_binding(
    period: dict[str, Any],
    plan: VerificationReverificationPlan,
    binding: VerificationReverificationBinding,
) -> None:
    """核验唯一切换、全部旧事实和同一上限；新未决仍由运行入口拒绝。"""

    prefix = period["requests"][: binding.prior_request_count]
    if (
        binding.period_id != plan.period_id
        or binding.reverification_id != plan.reverification_id
        or binding.previous_suite_id != plan.suite_id
        or binding.suite_id == plan.suite_id
        or binding.original_plan_sha256 != digest(plan.model_dump(mode="json"))
        or binding.prior_request_count < plan.prior_request_count
        or len(prefix) != binding.prior_request_count
        or digest(prefix) != binding.prior_requests_sha256
    ):
        raise ValueError
    # 切换只承接原授权声明的旧unknown，不能替新请求豁免未决。
    carried = {str(request.request_id) for request in plan.carried_requests}
    unknown = {request["request_id"] for request in prefix if request["status"] == "unknown"}
    if unknown != carried or any(
        request["status"] == "reserved" or "reverification_binding_id" in request
        for request in prefix
    ):
        raise ValueError
    charged = validate_reverification_plan({**period, "requests": prefix}, plan)
    if (
        charged != amount_units(binding.charged_cost)
        or amount_units(binding.remaining_cost) <= 0
        or charged + amount_units(binding.remaining_cost) != amount_units(plan.maximum_cost)
    ):
        raise ValueError
    for request in period["requests"][binding.prior_request_count :]:
        if request.get("reverification_binding_id") != str(binding.binding_id) or request.get(
            "suite_id"
        ) != str(binding.suite_id):
            raise ValueError
