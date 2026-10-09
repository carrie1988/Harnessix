"""单次有界复验授权合同；旧未决请求只可原样承接，不结算或退款。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, TypeAdapter, field_validator

from harnessix.domain.models import ContractModel
from harnessix.evals.cli_config import read_private_eval_config
from harnessix.models._json import strict_json
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


class _ReverificationPlanFields(ContractModel):
    """私有文件读器的共用字段；必须再经封闭版本解析才可登记。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    spec_version: str
    authority: Literal["budget-owner-explicit"]
    reverification_id: UUID
    suite_id: UUID
    period_id: UUID
    allocation: Amount
    maximum_cost: Amount
    ledger_before_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    carried_requests: Annotated[
        tuple[CarriedVerificationRequest, ...], Field(min_length=1, max_length=1)
    ]


class VerificationReverificationPlan(_ReverificationPlanFields):
    """预算所有者明确授予的唯一Suite；不增加原70元周期额度。"""

    spec_version: Literal["harnessix.provider-reverification-plan/v1"]
    allocation: Literal["70"]
    maximum_cost: Literal["40"]


class VerificationReverificationPlanV2(_ReverificationPlanFields):
    """60元原周期内的唯一38元复验；合同可用不表示实际授权已登记。"""

    spec_version: Literal["harnessix.provider-reverification-plan/v2"]
    allocation: Literal["60"]
    maximum_cost: Literal["38"]


class VerificationBetaTaskReverificationPlan(ContractModel):
    """原60元周期内只授予BETA-001新增5元；不是Eval Suite授权。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    spec_version: Literal["harnessix.provider-task-reverification-plan/v1"]
    authority: Literal["budget-owner-explicit"]
    reverification_id: UUID
    task_id: Literal["BETA-001"]
    period_id: UUID
    allocation: Literal["60"]
    maximum_cost: Literal["5"]
    ledger_before_sha256: Sha256
    prior_request_count: Annotated[int, Field(ge=1, le=10000)]
    prior_requests_sha256: Sha256
    carried_requests: Annotated[
        tuple[CarriedVerificationRequest, ...], Field(min_length=1, max_length=1)
    ]


class VerificationBetaTaskBudgetPlan(VerificationBetaTaskReverificationPlan):
    """空新60元周期内的BETA-001累计10元授权；不承接旧费用或限制请求次数。"""

    spec_version: Literal["harnessix.provider-beta-task-budget/v1"]
    maximum_cost: Literal["10"]
    prior_request_count: Literal[0]
    prior_requests_sha256: Literal[
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    ]
    carried_requests: tuple[()]

    @field_validator("prior_request_count", mode="before")
    @classmethod
    def validate_empty_prefix_count(cls, value: object) -> int:
        # Literal[0]本身会把False或0.0归一化为0，授权合同禁止这种隐式转换。
        if type(value) is not int:
            raise ValueError("旧请求数量必须为整数0")
        return value


type VerificationReverificationPlanRecord = (
    VerificationReverificationPlan
    | VerificationReverificationPlanV2
    | VerificationBetaTaskReverificationPlan
    | VerificationBetaTaskBudgetPlan
)

_PLAN_ADAPTER: TypeAdapter[VerificationReverificationPlanRecord] = TypeAdapter(
    Annotated[VerificationReverificationPlanRecord, Field(discriminator="spec_version")]
)


class _ReverificationPlanFile(_ReverificationPlanFields):
    """只作私有文件读入；保留显式null/混合字段供封闭版本解析拒绝。"""

    suite_id: UUID | None = None
    task_id: str | None = None
    prior_request_count: Annotated[int, Field(ge=0, le=10000)]
    carried_requests: Annotated[
        tuple[CarriedVerificationRequest, ...], Field(min_length=0, max_length=1)
    ]


def parse_reverification_plan(text: str) -> VerificationReverificationPlanRecord:
    """只接受完整Suite或Beta task版本，拒绝重复键、金额及身份混搭。"""
    strict_json(text)
    return _PLAN_ADAPTER.validate_json(text, strict=True)


def read_reverification_plan(path: str) -> VerificationReverificationPlanRecord:
    """沿用0600、有界、无链接私有读器；共用字段不能直接授予请求权限。"""
    fields = read_private_eval_config(path, _ReverificationPlanFile, max_bytes=64 * 1024)
    return parse_reverification_plan(fields.model_dump_json(exclude_unset=True))


def snapshot_reverification_plan(value: object) -> VerificationReverificationPlanRecord:
    """重建真实类型与嵌套字段，阻止copy或construct绕过封闭金额合同。"""
    if (
        type(value)
        not in {
            VerificationReverificationPlan,
            VerificationReverificationPlanV2,
            VerificationBetaTaskReverificationPlan,
            VerificationBetaTaskBudgetPlan,
        }
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
    return parse_reverification_plan(checked.model_dump_json())


def validate_reverification_plan(
    period: dict[str, Any], plan: VerificationReverificationPlanRecord
) -> int:
    """核验整个旧请求前缀不可变；累计新增已知费用与全额预留，不猜测未知费用。"""
    plan = snapshot_reverification_plan(plan)
    task_scoped = isinstance(plan, VerificationBetaTaskReverificationPlan)
    if task_scoped and any(
        field in period for field in ("reverification_binding", "reverification_binding_chain")
    ):
        raise ValueError
    requests = period["requests"]
    prefix = requests[: plan.prior_request_count]
    if (
        str(plan.period_id) != period["period_id"]
        or amount_units(period["allocation"]) != amount_units(plan.allocation)
        or len(prefix) != plan.prior_request_count
        or digest(prefix) != plan.prior_requests_sha256
        or any(
            r["status"] == "reserved" or "reverification_id" in r or "task_id" in r for r in prefix
        )
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
        if isinstance(plan, VerificationBetaTaskReverificationPlan):
            if (
                request.get("task_id") != plan.task_id
                or "suite_id" in request
                or "reverification_binding_id" in request
            ):
                raise ValueError
        elif "task_id" in request:
            raise ValueError
        charged += amount_units(request["reserved_cost"])
        if request["status"] in {"completed", "not_sent"}:
            charged += amount_units(request["cost_estimate"])
    if charged > amount_units(plan.maximum_cost):
        raise ValueError
    return charged
