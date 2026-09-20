from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.evals.contracts import CodingEvalEnvironment
from harnessix.evals.task_pack import LoadedCodingEvalTaskPack, builtin_coding_eval_task_pack
from harnessix.evals.task_pack_suite import (
    build_task_pack_offline_suite_config,
    build_task_pack_suite_config,
)
from harnessix.models.pricing import BillingContext, FlatInputPrice, PriceSnapshot

_SUITE_ID = UUID("79f70817-6fa0-5b4a-bf95-684b74f9fcb5")
_REVISION = "3bf7b7b05254da99a9b9c20618b3dab038836c31"
_CREATED_AT = datetime(2026, 9, 20, tzinfo=UTC)


def _config(tmp_path: Path, *, suite_id: UUID = _SUITE_ID):
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    return build_task_pack_offline_suite_config(
        loaded,
        suite_id=suite_id,
        work_root=tmp_path / "suite",
        harnessix_revision=_REVISION,
        platform="linux",
        created_at=_CREATED_AT,
    )


def test_offline_suite_config_is_deterministic_complete_and_zero_cost(tmp_path: Path) -> None:
    first = _config(tmp_path)
    second = _config(tmp_path)

    assert first == second
    assert len(first.plan.cases) == len(first.campaign_plans) == 10
    assert sum(len(campaign.run_ids) for campaign in first.campaign_plans) == 20
    assert Counter(case.task_kind for case in first.plan.cases) == {
        "bug_fix": 2,
        "feature": 2,
        "refactor": 2,
        "test": 2,
        "review": 2,
    }
    assert len({case.repository.name for case in first.plan.cases}) == 3
    assert tuple(case.case_id for case in first.plan.cases) == tuple(
        case.case_id
        for case in builtin_coding_eval_task_pack("harnessix-engineering", 2).manifest.cases
    )
    identities = tuple(
        identity
        for campaign in first.campaign_plans
        for identity in (campaign.campaign_id, *campaign.run_ids)
    )
    assert len(identities) == len(set(identities)) == 30
    assert all(identity.version == 5 for identity in identities)
    assert all(campaign.created_at == _CREATED_AT for campaign in first.campaign_plans)
    assert all(campaign.price.input_price.per_million == "0" for campaign in first.campaign_plans)
    assert all(campaign.price.output_per_million == "0" for campaign in first.campaign_plans)
    assert first.plan.environment.provider == "recorded"
    assert first.plan.environment.isolation == "fixed-container-no-network"
    assert first.fee_stop_amount == "1"


def test_offline_suite_identity_is_namespaced_by_suite_id(tmp_path: Path) -> None:
    first = _config(tmp_path / "first")
    second = _config(
        tmp_path / "second",
        suite_id=UUID("9bef21ee-6d64-55bf-91af-d0eed9f3e949"),
    )
    assert first.plan.suite_id != second.plan.suite_id
    assert tuple(case.task_fingerprint for case in first.plan.cases) == tuple(
        case.task_fingerprint for case in second.plan.cases
    )
    assert {
        identity
        for campaign in first.campaign_plans
        for identity in (campaign.campaign_id, *campaign.run_ids)
    }.isdisjoint(
        identity
        for campaign in second.campaign_plans
        for identity in (campaign.campaign_id, *campaign.run_ids)
    )


def test_general_suite_composer_preserves_real_provider_price_and_scope(tmp_path: Path) -> None:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    environment = CodingEvalEnvironment(
        harnessix_revision=_REVISION,
        provider="openai_chat",
        model="coder-v1",
        platform="linux",
        isolation="fixed-container-checks-provider-network",
    )
    price = PriceSnapshot(
        version="fixture-price-v1",
        source_url="https://pricing.invalid/coder-v1",
        billing_provider="fixture-cloud",
        model="coder-v1",
        region="fixture-region",
        service_tier="payg",
        inference_mode="non-thinking",
        currency="CNY",
        valid_from=datetime(2026, 9, 1, tzinfo=UTC),
        valid_until=datetime(2026, 10, 1, tzinfo=UTC),
        input_tokens_min=0,
        input_tokens_max=32_000,
        input_price=FlatInputPrice(per_million="4"),
        output_per_million="16",
    )
    billing = BillingContext(
        billing_provider="fixture-cloud",
        region="fixture-region",
        service_tier="payg",
        inference_mode="non-thinking",
    )

    config = build_task_pack_suite_config(
        loaded,
        suite_id=_SUITE_ID,
        work_root=tmp_path / "real-suite",
        environment=environment,
        price=price,
        billing_context=billing,
        fee_stop_amount="40",
        created_at=_CREATED_AT,
    )

    assert config.plan.environment == environment
    assert config.fee_stop_currency == "CNY" and config.fee_stop_amount == "40"
    assert all(campaign.price == price for campaign in config.campaign_plans)
    assert all(campaign.billing_context == billing for campaign in config.campaign_plans)

    with pytest.raises(ValueError, match="环境模型"):
        build_task_pack_suite_config(
            loaded,
            suite_id=_SUITE_ID,
            work_root=tmp_path / "invalid",
            environment=environment.model_copy(update={"model": "drifted"}),
            price=price,
            billing_context=billing,
            fee_stop_amount="40",
            created_at=_CREATED_AT,
        )


def test_offline_suite_revalidates_pack_and_rejects_invalid_host_fields(tmp_path: Path) -> None:
    loaded = builtin_coding_eval_task_pack("harnessix-engineering", 2)
    counterfeit = LoadedCodingEvalTaskPack(loaded.manifest, tmp_path)
    with pytest.raises(KernelError) as invalid_pack:
        build_task_pack_offline_suite_config(
            counterfeit,
            suite_id=_SUITE_ID,
            work_root=tmp_path / "suite",
            harnessix_revision=_REVISION,
            platform="linux",
            created_at=_CREATED_AT,
        )
    assert invalid_pack.value.code == "eval_task_pack_invalid"

    with pytest.raises(ValidationError, match="绝对路径"):
        build_task_pack_offline_suite_config(
            loaded,
            suite_id=_SUITE_ID,
            work_root=Path("relative-suite"),
            harnessix_revision=_REVISION,
            platform="linux",
            created_at=_CREATED_AT,
        )
    with pytest.raises(ValidationError):
        build_task_pack_offline_suite_config(
            loaded,
            suite_id=_SUITE_ID,
            work_root=tmp_path / "suite",
            harnessix_revision="not-a-revision",
            platform="linux",
            created_at=datetime(2026, 9, 20),
        )
