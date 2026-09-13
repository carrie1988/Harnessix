"""产品预检的配置、Profile、Provider依赖与凭据引用检查。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.codec import decode_product_config_bytes, read_product_config_bytes
from harnessix.product_config.contracts import (
    ConfigurationDiagnosticReport,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProfileSelection,
    product_config_digest,
)
from harnessix.product_config.preflight_support import PreflightRecorder
from harnessix.product_config.product_contracts import PreflightCategory
from harnessix.product_config.runtime import (
    DependencyFinder,
    diagnose_configuration,
    environment_secret_provider,
    select_profile,
)


@dataclass(frozen=True, slots=True)
class ConfigurationPreflight:
    """供后续环境检查和最终报告使用的配置检查结果。"""

    snapshot: ProductConfigSnapshot | None
    diagnostics: ConfigurationDiagnosticReport | None


def _read(path: Path, recorder: PreflightRecorder) -> bytes | None:
    began = recorder.started()
    try:
        body = read_product_config_bytes(path)
        recorder.record(
            "product_config_file",
            "config",
            "required",
            "passed",
            "product_config_file_ready",
            None,
            began,
        )
        return body
    except KernelError as error:
        recorder.record(
            "product_config_file",
            "config",
            "required",
            "failed",
            error.code,
            "run_configure",
            began,
        )
    except Exception:
        recorder.record(
            "product_config_file",
            "config",
            "required",
            "failed",
            "product_internal_failure",
            "run_configure",
            began,
        )
    return None


def _decode(body: bytes | None, recorder: PreflightRecorder) -> ProductConfigSnapshot | None:
    began = recorder.started()
    if body is None:
        recorder.record(
            "product_config_contract",
            "config",
            "required",
            "skipped",
            "product_config_prerequisite_failed",
            "run_configure",
            began,
        )
        return None
    try:
        document = decode_product_config_bytes(body, allow_legacy=True)
        if isinstance(document, ProductConfigV1):
            raise KernelError("product_config_migration_required", "产品配置必须先迁移到v2")
        snapshot = ProductConfigSnapshot(
            source_sha256=hashlib.sha256(body).hexdigest(),
            config_sha256=product_config_digest(document),
            loaded_at=utc_now(),
            config=document,
        )
        recorder.record(
            "product_config_contract",
            "config",
            "required",
            "passed",
            "product_config_contract_ready",
            None,
            began,
        )
        return snapshot
    except KernelError as error:
        recorder.record(
            "product_config_contract",
            "config",
            "required",
            "failed",
            error.code,
            "migrate_or_configure",
            began,
        )
    except Exception:
        recorder.record(
            "product_config_contract",
            "config",
            "required",
            "failed",
            "product_internal_failure",
            "migrate_or_configure",
            began,
        )
    return None


def _select(
    snapshot: ProductConfigSnapshot | None,
    profile_id: str | None,
    recorder: PreflightRecorder,
) -> ProfileSelection | None:
    began = recorder.started()
    if snapshot is None:
        recorder.record(
            "product_profile_selection",
            "profile",
            "required",
            "skipped",
            "product_config_prerequisite_failed",
            "select_profile",
            began,
        )
        return None
    try:
        selection = select_profile(snapshot, profile_id)
        recorder.record(
            "product_profile_selection",
            "profile",
            "required",
            "passed",
            "product_profile_ready",
            None,
            began,
        )
        return selection
    except KernelError as error:
        recorder.record(
            "product_profile_selection",
            "profile",
            "required",
            "failed",
            error.code,
            "select_profile",
            began,
        )
    except Exception:
        recorder.record(
            "product_profile_selection",
            "profile",
            "required",
            "failed",
            "product_internal_failure",
            "select_profile",
            began,
        )
    return None


def _provider_check(
    recorder: PreflightRecorder,
    diagnostics: ConfigurationDiagnosticReport | None,
    internal_failure: bool,
    *,
    check_id: str,
    category: PreflightCategory,
    scopes: frozenset[str],
    failure_code: str,
    remediation_id: str,
) -> None:
    began = recorder.started()
    if internal_failure:
        recorder.record(
            check_id,
            category,
            "required",
            "failed",
            "product_internal_failure",
            remediation_id,
            began,
        )
        return
    if diagnostics is None:
        recorder.record(
            check_id,
            category,
            "required",
            "skipped",
            "product_config_prerequisite_failed",
            remediation_id,
            began,
        )
        return
    selected = [item for item in diagnostics.checks if item.scope in scopes]
    passed = bool(selected) and all(item.status == "passed" for item in selected)
    recorder.record(
        check_id,
        category,
        "required",
        "passed" if passed else "failed",
        f"{check_id}_ready" if passed else failure_code,
        None if passed else remediation_id,
        began,
    )


def inspect_configuration(
    path: Path,
    profile_id: str | None,
    *,
    dependency_finder: DependencyFinder,
    environment: Mapping[str, str] | None,
    recorder: PreflightRecorder,
) -> ConfigurationPreflight:
    """检查配置读取、合同、Profile和Provider运行前依赖。"""

    snapshot = _decode(_read(path, recorder), recorder)
    selection = _select(snapshot, profile_id, recorder)
    diagnostics: ConfigurationDiagnosticReport | None = None
    internal_failure = False
    if snapshot is not None and selection is not None:
        try:
            diagnostics = diagnose_configuration(
                snapshot,
                selection,
                environment_secret_provider(snapshot.config, environment=environment),
                dependency_finder=dependency_finder,
            )
        except Exception:
            internal_failure = True
    _provider_check(
        recorder,
        diagnostics,
        internal_failure,
        check_id="product_provider_dependencies",
        category="dependency",
        scopes=frozenset({"dependency"}),
        failure_code="product_dependency_missing",
        remediation_id="install_provider_extra",
    )
    _provider_check(
        recorder,
        diagnostics,
        internal_failure,
        check_id="product_provider_secrets",
        category="secret",
        scopes=frozenset({"secret", "provider"}),
        failure_code="secret_unavailable",
        remediation_id="set_environment_secret",
    )
    return ConfigurationPreflight(snapshot=snapshot, diagnostics=diagnostics)
