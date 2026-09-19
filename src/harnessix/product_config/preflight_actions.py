"""Product Action配置与无状态能力的Preflight/Doctor检查。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.action_codec import load_product_action_config
from harnessix.product_config.action_contracts import (
    ProductActionCapabilityReport,
    ProductActionConfigSnapshot,
)
from harnessix.product_config.action_diagnostics import diagnose_product_actions
from harnessix.product_config.contracts import ProductConfigSnapshot
from harnessix.product_config.preflight_support import PreflightRecorder
from harnessix.product_config.product_contracts import PreflightPlatform
from harnessix.product_config.runtime import environment_secret_provider


@dataclass(frozen=True, slots=True)
class ActionPreflight:
    """供最终Preflight报告和启动重新加载使用的Action配置检查结果。"""

    snapshot: ProductActionConfigSnapshot | None
    capabilities: ProductActionCapabilityReport | None


def inspect_product_actions(
    path: Path | None,
    product_snapshot: ProductConfigSnapshot | None,
    *,
    platform: PreflightPlatform,
    environment: Mapping[str, str] | None,
    recorder: PreflightRecorder,
) -> ActionPreflight:
    """安全加载Action配置并生成脱敏能力报告；不创建State Root。"""

    snapshot = _load(path, recorder)
    if snapshot is None:
        _record_catalog_prerequisite(recorder, "product_action_config_prerequisite_failed")
        return ActionPreflight(None, None)
    if product_snapshot is None:
        _record_catalog_prerequisite(recorder, "product_config_prerequisite_failed")
        return ActionPreflight(snapshot, None)
    began = recorder.started()
    try:
        capabilities = diagnose_product_actions(
            snapshot.config,
            platform=platform,
            secrets=environment_secret_provider(product_snapshot.config, environment=environment),
        )
    except Exception:
        recorder.record(
            "product_action_capability_catalog",
            "action",
            "required",
            "failed",
            "product_internal_failure",
            "inspect_action_capabilities",
            began,
        )
        return ActionPreflight(snapshot, None)
    recorder.record(
        "product_action_capability_catalog",
        "action",
        "required",
        "passed",
        "product_action_capability_catalog_ready",
        None,
        began,
    )
    for evidence in capabilities.capabilities:
        verified = evidence.status == "verified"
        recorder.record(
            _capability_check_id(evidence.capability_id),
            "action",
            "advisory",
            "passed" if verified else "failed",
            "product_action_capability_ready" if verified else evidence.reason_code,
            None if verified else _remediation(evidence.reason_code),
            began,
        )
    return ActionPreflight(snapshot, capabilities)


def _load(path: Path | None, recorder: PreflightRecorder) -> ProductActionConfigSnapshot | None:
    began = recorder.started()
    try:
        snapshot = load_product_action_config(path)
        recorder.record(
            "product_action_config_source",
            "action",
            "required",
            "passed",
            (
                "product_action_config_builtin"
                if path is None
                else "product_action_config_file_ready"
            ),
            None,
            began,
        )
        return snapshot
    except KernelError as error:
        recorder.record(
            "product_action_config_source",
            "action",
            "required",
            "failed",
            error.code,
            "fix_action_config",
            began,
        )
    except Exception:
        recorder.record(
            "product_action_config_source",
            "action",
            "required",
            "failed",
            "product_internal_failure",
            "fix_action_config",
            began,
        )
    return None


def _record_catalog_prerequisite(recorder: PreflightRecorder, code: str) -> None:
    recorder.record(
        "product_action_capability_catalog",
        "action",
        "required",
        "skipped",
        code,
        "inspect_action_capabilities",
        recorder.started(),
    )


def _capability_check_id(capability_id: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "_", capability_id.casefold()).strip("_")[:72]
    suffix = hashlib.sha256(capability_id.encode("utf-8")).hexdigest()[:8]
    return f"product_action_{readable}_{suffix}"


def _remediation(reason_code: str) -> str:
    mappings = {
        "disabled": "enable_action_capability",
        "platform_not_supported": "use_supported_platform",
        "container_engine_unsupported": "install_supported_container_engine",
        "container_unavailable": "start_container_engine",
        "container_image_unavailable": "install_pinned_container_image",
        "profile_limits_unenforceable": "fix_action_profile_limits",
        "process_owner_unavailable": "repair_process_owner",
        "secret_target_invalid": "fix_action_secret_target",
        "secret_unavailable": "set_environment_secret",
        "secret_version_changed": "update_action_secret_version",
        "profile_probe_failed": "inspect_action_profile",
    }
    return mappings.get(reason_code, "inspect_action_capability")
