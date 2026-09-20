"""把已核验Task Pack确定性组合为现有离线Suite执行配置。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.evals.campaign_contracts import CodingEvalCampaignPlan
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.suite_contracts import CodingEvalSuiteCasePlan, CodingEvalSuitePlan
from harnessix.evals.suite_execution_contracts import CodingEvalSuiteRunConfig
from harnessix.evals.task_pack import LoadedCodingEvalTaskPack, _verified_builtin_task_pack
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.models.pricing import (
    Amount,
    BillingContext,
    FlatInputPrice,
    PriceSnapshot,
)

_RECORDED_MODEL = "harnessix-recorded-v1"
_PRICE_VERSION = "task-pack-recorded-v1"


def _recorded_price() -> PriceSnapshot:
    return PriceSnapshot(
        version=_PRICE_VERSION,
        source_url="https://harnessix.invalid/pricing/recorded-v1",
        billing_provider="harnessix-recorded",
        model=_RECORDED_MODEL,
        region="offline",
        service_tier="eval",
        inference_mode="recorded",
        currency="USD",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        input_tokens_min=0,
        input_tokens_max=None,
        input_price=FlatInputPrice(per_million="0"),
        output_per_million="0",
    )


def _recorded_billing() -> BillingContext:
    return BillingContext(
        billing_provider="harnessix-recorded",
        region="offline",
        service_tier="eval",
        inference_mode="recorded",
    )


def _identity(suite_id: UUID, pack_id: str, pack_version: int, suffix: str) -> UUID:
    return uuid5(suite_id, f"task-pack:{pack_id}:{pack_version}:{suffix}")


def _campaign_plan(
    case: CodingEvalTaskPackCase,
    *,
    suite_id: UUID,
    pack_id: str,
    pack_version: int,
    environment: CodingEvalEnvironment,
    price: PriceSnapshot,
    billing_context: BillingContext,
    created_at: datetime,
) -> CodingEvalCampaignPlan:
    prefix = f"case:{case.case_id}"
    return CodingEvalCampaignPlan(
        campaign_id=_identity(suite_id, pack_id, pack_version, f"{prefix}:campaign"),
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        environment=environment,
        run_ids=tuple(
            _identity(suite_id, pack_id, pack_version, f"{prefix}:trial:{index}")
            for index in (1, 2)
        ),
        price=price,
        billing_context=billing_context,
        created_at=created_at,
    )


def _case_plan(
    case: CodingEvalTaskPackCase,
    campaign: CodingEvalCampaignPlan,
) -> CodingEvalSuiteCasePlan:
    return CodingEvalSuiteCasePlan(
        case_id=case.case_id,
        task_kind=case.task_kind,
        task_id=case.task.task_id,
        task_version=case.task.task_version,
        task_fingerprint=case.task.fingerprint,
        repository=case.task.repository,
        campaign_plan_fingerprint=campaign.fingerprint,
    )


def build_task_pack_suite_config(
    loaded: LoadedCodingEvalTaskPack,
    *,
    suite_id: UUID,
    work_root: Path,
    environment: CodingEvalEnvironment,
    price: PriceSnapshot,
    billing_context: BillingContext,
    fee_stop_amount: Amount,
    created_at: datetime,
) -> CodingEvalSuiteRunConfig:
    """为内置Pack构造每Case两个Trial的确定性Suite配置。"""

    verified = _verified_builtin_task_pack(loaded)
    manifest = verified.manifest
    if environment.model != price.model:
        raise ValueError("Suite环境模型与价格快照不一致")
    for field in ("billing_provider", "region", "service_tier", "inference_mode"):
        if getattr(billing_context, field) != getattr(price, field):
            raise ValueError("Suite计费上下文与价格快照不一致")
    campaigns = tuple(
        _campaign_plan(
            case,
            suite_id=suite_id,
            pack_id=manifest.pack_id,
            pack_version=manifest.pack_version,
            environment=environment,
            price=price,
            billing_context=billing_context,
            created_at=created_at,
        )
        for case in manifest.cases
    )
    plan = CodingEvalSuitePlan(
        suite_id=suite_id,
        suite_version=1,
        environment=environment,
        cases=tuple(
            _case_plan(case, campaign)
            for case, campaign in zip(manifest.cases, campaigns, strict=True)
        ),
        created_at=created_at,
    )
    return CodingEvalSuiteRunConfig(
        plan=plan,
        campaign_plans=campaigns,
        work_root=str(work_root),
        fee_stop_currency=price.currency,
        fee_stop_amount=fee_stop_amount,
    )


def build_task_pack_offline_suite_config(
    loaded: LoadedCodingEvalTaskPack,
    *,
    suite_id: UUID,
    work_root: Path,
    harnessix_revision: str,
    platform: str,
    created_at: datetime,
) -> CodingEvalSuiteRunConfig:
    """为内置Pack构造每Case两个Trial的稳定零费用Recorded Suite。"""

    return build_task_pack_suite_config(
        loaded,
        suite_id=suite_id,
        work_root=work_root,
        environment=CodingEvalEnvironment(
            harnessix_revision=harnessix_revision,
            provider="recorded",
            model=_RECORDED_MODEL,
            platform=platform,
            isolation="fixed-container-no-network",
        ),
        price=_recorded_price(),
        billing_context=_recorded_billing(),
        fee_stop_amount="1",
        created_at=created_at,
    )
