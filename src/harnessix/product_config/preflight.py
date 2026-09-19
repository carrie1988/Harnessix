"""产品启动预检：编排配置与环境检查并生成共享只读事实报告。"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harnessix.domain.models import utc_now
from harnessix.product_config.preflight_actions import inspect_product_actions
from harnessix.product_config.preflight_configuration import inspect_configuration
from harnessix.product_config.preflight_environment import (
    WorkspaceProbe,
    default_workspace_probe,
    inspect_environment,
)
from harnessix.product_config.preflight_support import (
    MonotonicClock,
    PreflightRecorder,
    native_platform,
    workspace_fingerprint,
)
from harnessix.product_config.product_contracts import (
    PreflightMode,
    ProductPreflightReport,
    product_preflight_report_digest,
)
from harnessix.product_config.runtime import DependencyFinder


@dataclass(frozen=True, slots=True)
class ProductPreflightRequest:
    """一次离线产品检查所需的全部显式路径、选择和能力要求。"""

    mode: PreflightMode
    config_path: Path
    action_config_path: Path | None
    profile_id: str | None
    workspace: Path
    state_directory: Path
    git_executable: Path | None
    require_tui: bool = True


def run_product_preflight(
    request: ProductPreflightRequest,
    *,
    dependency_finder: DependencyFinder = importlib.util.find_spec,
    environment: Mapping[str, str] | None = None,
    workspace_probe: WorkspaceProbe = default_workspace_probe,
    platform_name: str | None = None,
    monotonic_clock: MonotonicClock = time.monotonic,
) -> ProductPreflightReport:
    """执行离线只读检查；预期失败转为稳定Check并继续独立检查。"""

    platform = native_platform(platform_name)
    recorder = PreflightRecorder(monotonic_clock)
    configuration = inspect_configuration(
        request.config_path,
        request.profile_id,
        dependency_finder=dependency_finder,
        environment=environment,
        recorder=recorder,
    )
    actions = inspect_product_actions(
        request.action_config_path,
        configuration.snapshot,
        platform=platform,
        environment=environment,
        recorder=recorder,
    )
    inspect_environment(
        workspace=request.workspace,
        config_path=request.config_path,
        config_loaded=configuration.snapshot is not None,
        action_config_path=request.action_config_path,
        action_config_loaded=actions.snapshot is not None,
        state_directory=request.state_directory,
        git_executable=request.git_executable,
        require_tui=request.require_tui,
        platform=platform,
        platform_name=platform_name,
        workspace_probe=workspace_probe,
        dependency_finder=dependency_finder,
        recorder=recorder,
    )
    checks = recorder.ordered()
    diagnostics = configuration.diagnostics
    ready = (
        diagnostics is not None
        and diagnostics.ready
        and all(item.status == "passed" for item in checks if item.requirement == "required")
    )
    candidate = ProductPreflightReport.model_construct(
        mode=request.mode,
        platform=platform,
        workspace_fingerprint=workspace_fingerprint(request.workspace),
        config_sha256=diagnostics.config_sha256 if diagnostics is not None else None,
        selected_profile=diagnostics.selected_profile if diagnostics is not None else None,
        configuration=diagnostics,
        action_config_sha256=(
            actions.snapshot.config_sha256 if actions.snapshot is not None else None
        ),
        actions=actions.capabilities,
        checks=checks,
        ready=ready,
        generated_at=utc_now(),
        report_sha256="0" * 64,
    )
    return ProductPreflightReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=product_preflight_report_digest(candidate),
    )
