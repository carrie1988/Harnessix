from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.evals.provider_suite_contracts import (
    CodingEvalProviderSuiteEvidenceManifest,
    CodingEvalProviderSuiteRunConfig,
    CodingEvalProviderSuiteRunReport,
)
from tests.evals.provider_suite_helpers import provider_suite_config


def test_provider_suite_config_binds_complete_pack_provider_price_and_budget(
    tmp_path: Path,
) -> None:
    config = provider_suite_config(tmp_path)

    assert len(config.suite.plan.cases) == 10
    assert sum(len(plan.run_ids) for plan in config.suite.campaign_plans) == 20
    assert config.suite.fee_stop_amount == "40"
    assert config.provider_config.max_attempts == 1
    assert config.provider_config.capabilities.parallel_tool_calls is False
    assert len(config.fingerprint) == 64
    assert config.provider_config.api_key_env == "DASHSCOPE_API_KEY"
    assert "api_key" not in type(config.provider_config).model_fields


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model", "another-model", "环境与Provider"),
        ("max_attempts", 2, "禁止Provider自动重试"),
        ("max_output_tokens", 4097, "输出上限"),
    ],
)
def test_provider_suite_config_rejects_execution_drift(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    config = provider_suite_config(tmp_path)
    provider = config.provider_config.model_copy(update={field: value})

    with pytest.raises(ValidationError, match=message):
        CodingEvalProviderSuiteRunConfig.model_validate(
            {**config.model_dump(mode="python"), "provider_config": provider},
            strict=True,
        )


def test_provider_suite_run_report_hides_identity_until_config_is_accepted() -> None:
    disabled = CodingEvalProviderSuiteRunReport(reason="network_not_enabled")
    assert disabled.suite_id is None and disabled.scheduled_cases == 0

    with pytest.raises(ValidationError, match="不能携带Suite身份"):
        CodingEvalProviderSuiteRunReport(
            reason="configuration_invalid",
            suite_id=provider_suite_config(Path("/tmp/provider-suite")).suite.plan.suite_id,
            scheduled_cases=10,
        )


def test_provider_suite_evidence_manifest_is_strict_and_cost_bound(tmp_path: Path) -> None:
    config = provider_suite_config(tmp_path)
    price = config.suite.campaign_plans[0].price
    payload = {
        "suite_id": config.suite.plan.suite_id,
        "pack_id": config.pack_id,
        "pack_version": config.pack_version,
        "pack_sha256": config.pack_sha256,
        "harnessix_revision": config.suite.plan.environment.harnessix_revision,
        "model": config.provider_config.model,
        "region": price.region,
        "price_sha256": price.digest,
        "pricing_source_url": price.source_url,
        "plan_fingerprint": config.suite.plan.fingerprint,
        "report_sha256": "f" * 64,
        "scheduled_cases": 10,
        "scheduled_trials": 20,
        "passed_trials": 12,
        "tests_passed_trials": 14,
        "human_intervention_trials": 0,
        "model_attempts": 100,
        "input_tokens": 1000,
        "output_tokens": 200,
        "known_cost_currency": "CNY",
        "known_cost_amount": "1.23",
        "fee_stop_currency": "CNY",
        "fee_stop_amount": "40",
        "completed_at": config.suite.plan.created_at,
    }
    manifest = CodingEvalProviderSuiteEvidenceManifest.model_validate(payload, strict=True)
    assert manifest.cost_completeness == "complete"
    with pytest.raises(ValidationError):
        CodingEvalProviderSuiteEvidenceManifest.model_validate(
            {**payload, "secret": "forbidden"}, strict=True
        )
    with pytest.raises(ValidationError, match="币种不一致"):
        CodingEvalProviderSuiteEvidenceManifest.model_validate(
            {**payload, "fee_stop_currency": "USD"}, strict=True
        )
