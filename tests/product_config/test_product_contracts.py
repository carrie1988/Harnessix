from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from harnessix.product_config.contracts import (
    ConfigurationDiagnostic,
    ConfigurationDiagnosticReport,
    diagnostic_report_digest,
)
from harnessix.product_config.product_contracts import (
    ConfigurationDraft,
    ConfigurationWriteReceipt,
    ProductPreflightCheck,
    ProductPreflightReport,
    configuration_write_receipt_digest,
    product_preflight_report_digest,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _configuration_report(*, ready: bool = True) -> ConfigurationDiagnosticReport:
    check = ConfigurationDiagnostic(
        scope="config",
        subject_id="document",
        code="config_contract_valid",
        status="passed" if ready else "failed",
    )
    candidate = ConfigurationDiagnosticReport.model_construct(
        config_sha256=_DIGEST_A,
        selection_sha256=_DIGEST_B,
        selected_profile="primary",
        ready=ready,
        checks=(check,),
        generated_at=_NOW,
        report_sha256="0" * 64,
    )
    return ConfigurationDiagnosticReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=diagnostic_report_digest(candidate),
    )


def _preflight_report(
    *,
    configuration: ConfigurationDiagnosticReport | None,
    checks: tuple[ProductPreflightCheck, ...],
) -> ProductPreflightReport:
    candidate = ProductPreflightReport.model_construct(
        mode="doctor",
        platform="posix",
        workspace_fingerprint="c" * 64,
        config_sha256=configuration.config_sha256 if configuration else None,
        selected_profile=configuration.selected_profile if configuration else None,
        configuration=configuration,
        checks=checks,
        ready=(
            configuration is not None
            and configuration.ready
            and all(item.status == "passed" for item in checks if item.requirement == "required")
        ),
        generated_at=_NOW,
        report_sha256="0" * 64,
    )
    return ProductPreflightReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=product_preflight_report_digest(candidate),
    )


def test_configuration_draft_rejects_anthropic_openai_token_parameter() -> None:
    with pytest.raises(ValidationError):
        ConfigurationDraft(
            provider_kind="anthropic",
            base_url="https://api.anthropic.test",
            model="claude-test",
            output_token_parameter="max_tokens",
        )


def test_configuration_write_receipt_binds_operation_previous_digest_and_hash() -> None:
    candidate = ConfigurationWriteReceipt.model_construct(
        operation="created",
        previous_source_sha256=None,
        source_sha256=_DIGEST_A,
        config_sha256=_DIGEST_B,
        occurred_at=_NOW,
        receipt_sha256="0" * 64,
    )
    receipt = ConfigurationWriteReceipt(
        **candidate.model_dump(exclude={"receipt_sha256"}),
        receipt_sha256=configuration_write_receipt_digest(candidate),
    )
    assert receipt.operation == "created"

    with pytest.raises(ValidationError):
        ConfigurationWriteReceipt(**receipt.model_dump() | {"operation": "replaced"})

    with pytest.raises(ValidationError):
        ConfigurationWriteReceipt(
            **receipt.model_dump(exclude={"receipt_sha256"}), receipt_sha256=_DIGEST_A
        )


def test_product_preflight_report_requires_sorted_unique_checks() -> None:
    configuration = _configuration_report()
    checks = (
        ProductPreflightCheck(
            check_id="product_workspace_binding",
            category="workspace",
            requirement="required",
            status="passed",
            code="workspace_ready",
            duration_ms=1,
        ),
        ProductPreflightCheck(
            check_id="product_config_contract",
            category="config",
            requirement="required",
            status="passed",
            code="config_ready",
            duration_ms=1,
        ),
    )
    with pytest.raises(ValidationError):
        _preflight_report(configuration=configuration, checks=checks)


def test_product_preflight_ready_ignores_advisory_but_not_required_failure() -> None:
    configuration = _configuration_report()
    advisory_failure = ProductPreflightCheck(
        check_id="product_git_binding",
        category="git",
        requirement="advisory",
        status="failed",
        code="product_git_unavailable",
        remediation_id="omit_or_install_git",
        duration_ms=0,
    )
    report = _preflight_report(configuration=configuration, checks=(advisory_failure,))
    assert report.ready is True

    required_failure = ProductPreflightCheck(
        check_id="product_workspace_binding",
        category="workspace",
        requirement="required",
        status="failed",
        code="workspace_invalid",
        remediation_id="inspect_workspace",
        duration_ms=0,
    )
    invalid_candidate = report.model_copy(
        update={
            "checks": (advisory_failure, required_failure),
            "report_sha256": "0" * 64,
        }
    )
    with pytest.raises(ValidationError):
        ProductPreflightReport(
            **invalid_candidate.model_dump(exclude={"report_sha256"}),
            report_sha256=product_preflight_report_digest(invalid_candidate),
        )


def test_product_preflight_binds_configuration_identity_and_digest() -> None:
    report = _preflight_report(
        configuration=_configuration_report(),
        checks=(
            ProductPreflightCheck(
                check_id="product_config_contract",
                category="config",
                requirement="required",
                status="passed",
                code="config_ready",
                duration_ms=0,
            ),
        ),
    )
    with pytest.raises(ValidationError):
        ProductPreflightReport(
            **report.model_dump(exclude={"config_sha256"}),
            config_sha256=_DIGEST_B,
        )
    with pytest.raises(ValidationError):
        ProductPreflightReport(
            **report.model_dump(exclude={"report_sha256"}),
            report_sha256=_DIGEST_A,
        )
