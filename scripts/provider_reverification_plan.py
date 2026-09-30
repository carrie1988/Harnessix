"""单次有界复验授权合同；旧未决请求只可原样承接，不结算或退款。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from harnessix.domain.models import ContractModel
from harnessix.models.pricing import amount_units
from harnessix.tools.workspace import digest

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Amount = Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]{1,18})?$")]


class CarriedVerificationRequest(ContractModel):
    """唯一旧未决请求的身份、完整记录摘要与不释放的预留。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: UUID
    reserved_cost: Amount
    request_sha256: Sha256


class VerificationReverificationPlan(ContractModel):
    """预算所有者明确授予的唯一Suite；不增加原70元周期额度。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    spec_version: Literal["harnessix.provider-reverification-plan/v1"]
    authority: Literal["budget-owner-explicit"]
    reverification_id: UUID
    suite_id: UUID
    period_id: UUID
    allocation: Literal["70"]
    maximum_cost: Literal["40"]
    ledger_before_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    carried_requests: Annotated[
        tuple[CarriedVerificationRequest, ...], Field(min_length=1, max_length=1)
    ]


def validate_reverification_plan(
    period: dict[str, Any], plan: VerificationReverificationPlan
) -> int:
    """核验整个旧请求前缀不可变；累计新增已知费用与全额预留，不猜测未知费用。"""
    requests = period["requests"]
    prefix = requests[: plan.prior_request_count]
    if (
        str(plan.period_id) != period["period_id"]
        or amount_units(period["allocation"]) != amount_units(plan.allocation)
        or len(prefix) != plan.prior_request_count
        or digest(prefix) != plan.prior_requests_sha256
        or any(r["status"] == "reserved" or "reverification_id" in r for r in prefix)
    ):
        raise ValueError
    unknown = {r["request_id"]: r for r in prefix if r["status"] == "unknown"}
    carried = {str(r.request_id): r for r in plan.carried_requests}
    if set(unknown) != set(carried):
        raise ValueError
    for identity, request in unknown.items():
        reference = carried[identity]
        if (
            reference.reserved_cost != request["reserved_cost"]
            or amount_units(reference.reserved_cost) <= 0
            or reference.request_sha256 != digest(request)
        ):
            raise ValueError
    charged = 0
    for request in requests[plan.prior_request_count :]:
        if request.get("reverification_id") != str(plan.reverification_id):
            raise ValueError
        charged += amount_units(request["reserved_cost"])
        if request["status"] in {"completed", "not_sent"}:
            charged += amount_units(request["cost_estimate"])
    if charged > amount_units(plan.maximum_cost):
        raise ValueError
    return charged
