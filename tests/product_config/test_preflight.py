from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.contracts import (
    ProductConfigV2,
    ProductPreflightCheck,
    ProductPreflightReport,
)
from harnessix.product_config.preflight import ProductPreflightRequest, run_product_preflight
from tests.product_config.conftest import write_config
from tests.product_config.test_migration_and_store import legacy_body

_ENVIRONMENT = {
    "PRIMARY_API_KEY": "primary-secret",
    "BACKUP_API_KEY": "backup-secret",
}


def _request(
    tmp_path: Path,
    config_path: Path,
    *,
    profile_id: str | None = None,
    require_tui: bool = True,
    git_executable: Path | None = None,
) -> ProductPreflightRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ProductPreflightRequest(
        mode="doctor",
        config_path=config_path,
        profile_id=profile_id,
        workspace=workspace,
        state_directory=tmp_path / "state",
        git_executable=git_executable,
        require_tui=require_tui,
    )


def _dependencies(name: str) -> object | None:
    return object() if name in {"openai", "anthropic", "textual"} else None


def _checks(report: ProductPreflightReport) -> dict[str, ProductPreflightCheck]:
    return {item.check_id: item for item in report.checks}


def test_ready_report_is_ordered_hashed_redacted_and_read_only(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    canary = "preflight-secret-canary"
    environment = {**_ENVIRONMENT, "PRIMARY_API_KEY": canary}
    path = write_config(tmp_path / "config.json", config)
    request = _request(tmp_path, path)

    report = run_product_preflight(
        request,
        dependency_finder=_dependencies,
        environment=environment,
        monotonic_clock=lambda: 10.0,
    )

    assert report.ready is True
    assert [item.check_id for item in report.checks] == sorted(
        item.check_id for item in report.checks
    )
    assert all(item.duration_ms == 0 for item in report.checks)
    assert report.configuration is not None and report.configuration.ready
    assert not request.state_directory.exists()
    encoded = report.model_dump_json()
    assert canary not in encoded
    assert str(tmp_path) not in encoded
    assert len(report.report_sha256) == len(report.workspace_fingerprint) == 64


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_code"),
    [
        ("missing", "failed", "product_config_unavailable"),
        ("invalid", "failed", "product_config_invalid"),
        ("legacy", "failed", "product_config_migration_required"),
    ],
)
def test_reports_missing_invalid_and_legacy_configuration(
    tmp_path: Path,
    config: ProductConfigV2,
    kind: str,
    expected_status: str,
    expected_code: str,
) -> None:
    path = tmp_path / "config.json"
    if kind == "invalid":
        path.write_text("{invalid", encoding="utf-8")
        path.chmod(0o600)
    elif kind == "legacy":
        path.write_bytes(legacy_body(config))
        path.chmod(0o600)

    report = run_product_preflight(
        _request(tmp_path, path),
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )
    checks = _checks(report)

    target = (
        checks["product_config_file"] if kind == "missing" else checks["product_config_contract"]
    )
    assert target.status == expected_status and target.code == expected_code
    assert checks["product_profile_selection"].status == "skipped"
    assert report.configuration is None and report.ready is False
    assert not (tmp_path / "state").exists()


def test_profile_dependency_and_secret_failures_remain_distinct(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    path = write_config(tmp_path / "config.json", config)
    missing_profile = run_product_preflight(
        _request(tmp_path, path, profile_id="missing"),
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )
    profile_checks = _checks(missing_profile)
    assert profile_checks["product_profile_selection"].code == "product_profile_not_found"
    assert profile_checks["product_provider_dependencies"].status == "skipped"

    missing_dependency = run_product_preflight(
        _request(tmp_path, path),
        dependency_finder=lambda name: object() if name == "textual" else None,
        environment=_ENVIRONMENT,
    )
    dependency_checks = _checks(missing_dependency)
    assert dependency_checks["product_provider_dependencies"].code == ("product_dependency_missing")
    assert dependency_checks["product_provider_secrets"].status == "passed"

    missing_secret = run_product_preflight(
        _request(tmp_path, path),
        dependency_finder=_dependencies,
        environment={},
    )
    secret_checks = _checks(missing_secret)
    assert secret_checks["product_provider_dependencies"].status == "passed"
    assert secret_checks["product_provider_secrets"].code == "secret_unavailable"


def test_workspace_and_state_boundaries_fail_closed(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside_config = write_config(workspace / "config.json", config)
    request = ProductPreflightRequest(
        mode="startup",
        config_path=inside_config,
        profile_id=None,
        workspace=workspace,
        state_directory=tmp_path / "state",
        git_executable=None,
    )
    report = run_product_preflight(
        request,
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )
    assert _checks(report)["product_workspace_binding"].code == "product_config_overlap"
    assert _checks(report)["product_state_layout"].status == "passed"
    assert report.ready is False

    outside_config = write_config(tmp_path / "outside.json", config)
    overlap = replace(
        request,
        config_path=outside_config,
        state_directory=workspace / "state",
    )
    overlap_report = run_product_preflight(
        overlap,
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )
    assert _checks(overlap_report)["product_state_layout"].code == "product_state_overlap"


def test_existing_state_directory_must_remain_private(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX私有目录权限语义")
    path = write_config(tmp_path / "config.json", config)
    request = _request(tmp_path, path)
    request.state_directory.mkdir(mode=0o700)
    request.state_directory.chmod(0o755)

    report = run_product_preflight(
        request,
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )

    state = _checks(report)["product_state_layout"]
    assert state.status == "failed" and state.code == "product_state_invalid"


def test_tui_and_git_requirement_semantics(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    path = write_config(tmp_path / "config.json", config)

    required_tui = run_product_preflight(
        _request(tmp_path, path),
        dependency_finder=lambda name: None if name == "textual" else object(),
        environment=_ENVIRONMENT,
    )
    assert _checks(required_tui)["product_tui_dependency"].requirement == "required"
    assert required_tui.ready is False

    optional_tui = run_product_preflight(
        _request(tmp_path, path, require_tui=False),
        dependency_finder=lambda name: None if name == "textual" else object(),
        environment=_ENVIRONMENT,
    )
    assert _checks(optional_tui)["product_tui_dependency"].status == "skipped"
    assert optional_tui.ready is True
    assert _checks(optional_tui)["product_git_binding"].requirement == "advisory"

    executable = tmp_path / "git"
    executable.write_text("binary", encoding="utf-8")
    explicit_git = run_product_preflight(
        _request(tmp_path, path, git_executable=executable),
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )
    git_check = _checks(explicit_git)["product_git_binding"]
    if os.name == "nt":
        assert git_check.code == "product_git_platform_unsupported"
    else:
        assert git_check.status == "passed" and git_check.requirement == "required"


def test_windows_explicit_git_is_rejected_without_exposing_path(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    path = write_config(tmp_path / "config.json", config)
    report = run_product_preflight(
        _request(tmp_path, path, git_executable=tmp_path / "secret-git-path"),
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
        platform_name="windows",
        workspace_probe=lambda workspace, _platform: workspace.resolve(strict=True),
    )
    check = _checks(report)["product_git_binding"]
    assert check.status == "failed" and check.code == "product_git_platform_unsupported"
    assert "secret-git-path" not in report.model_dump_json()


def test_unknown_probe_failure_is_redacted_and_independent_checks_continue(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    path = write_config(tmp_path / "config.json", config)
    canary = "unexpected-secret-exception"

    def dependency(name: str) -> object:
        if name in {"openai", "anthropic"}:
            raise RuntimeError(canary)
        return object()

    report = run_product_preflight(
        _request(tmp_path, path),
        dependency_finder=dependency,
        environment=_ENVIRONMENT,
        workspace_probe=lambda *_: (_ for _ in ()).throw(RuntimeError(canary)),
    )
    checks = _checks(report)
    assert checks["product_provider_dependencies"].code == "product_internal_failure"
    assert checks["product_provider_secrets"].code == "product_internal_failure"
    assert checks["product_workspace_binding"].code == "product_internal_failure"
    assert checks["product_tui_dependency"].status == "passed"
    assert canary not in report.model_dump_json()


def test_unknown_platform_fails_before_constructing_ambiguous_report(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as error:
        run_product_preflight(
            _request(tmp_path, tmp_path / "config.json"),
            platform_name="unsupported",
        )
    assert error.value.code == "product_tools_platform_unsupported"


def test_report_json_round_trip_preserves_digest(
    tmp_path: Path,
    config: ProductConfigV2,
) -> None:
    path = write_config(tmp_path / "config.json", config)
    report = run_product_preflight(
        _request(tmp_path, path),
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
    )
    decoded = type(report).model_validate_json(json.dumps(report.model_dump(mode="json")))
    assert decoded == report
