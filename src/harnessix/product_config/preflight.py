"""产品启动预检：生成Preflight与Doctor共享的只读事实报告。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.codec import (
    decode_product_config_bytes,
    read_product_config_bytes,
)
from harnessix.product_config.contracts import (
    ConfigurationDiagnosticReport,
    PreflightCategory,
    PreflightMode,
    PreflightPlatform,
    PreflightRequirement,
    PreflightStatus,
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductPreflightCheck,
    ProductPreflightReport,
    product_config_digest,
    product_preflight_report_digest,
)
from harnessix.product_config.runtime import (
    DependencyFinder,
    diagnose_configuration,
    environment_secret_provider,
    select_profile,
)
from harnessix.workspace.snapshot import SecureWorkspaceReader

WorkspaceProbe = Callable[[Path, PreflightPlatform], Path]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ProductPreflightRequest:
    mode: PreflightMode
    config_path: Path
    profile_id: str | None
    workspace: Path
    state_directory: Path
    git_executable: Path | None
    require_tui: bool = True


def _workspace_fingerprint(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(
        b"harnessix-product-preflight-workspace/v1\0"
        + normalized.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _platform(value: str | None) -> PreflightPlatform:
    selected = value or os.name
    if selected == "posix":
        return "posix"
    if selected == "nt" or selected == "windows":
        return "windows"
    raise KernelError("product_tools_platform_unsupported", "产品运行平台不受支持")


def _duration_ms(started: float, clock: MonotonicClock) -> int:
    try:
        elapsed = max(0.0, clock() - started)
    except Exception:
        return 0
    return min(300_000, int(elapsed * 1000))


def _check(
    *,
    check_id: str,
    category: PreflightCategory,
    requirement: PreflightRequirement,
    status: PreflightStatus,
    code: str,
    remediation_id: str | None,
    started: float,
    clock: MonotonicClock,
) -> ProductPreflightCheck:
    return ProductPreflightCheck(
        check_id=check_id,
        category=category,
        requirement=requirement,
        status=status,
        code=code,
        remediation_id=remediation_id,
        duration_ms=_duration_ms(started, clock),
    )


def _default_workspace_probe(path: Path, platform: PreflightPlatform) -> Path:
    with SecureWorkspaceReader(path, platform=platform) as reader:
        return reader.path


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _state_layout(path: Path, workspace: Path) -> None:
    candidate = Path(os.path.abspath(path))
    if (
        candidate == workspace
        or candidate.is_relative_to(workspace)
        or workspace.is_relative_to(candidate)
    ):
        raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")

    current = candidate
    missing: list[str] = []
    while not current.exists() and not _is_link_or_junction(current):
        if current.parent == current:
            raise KernelError("product_state_invalid", "产品状态目录不可用")
        missing.append(current.name)
        current = current.parent
    try:
        info = current.lstat()
        if _is_link_or_junction(current) or not stat.S_ISDIR(info.st_mode):
            raise OSError
        resolved = current.resolve(strict=True)
        for part in reversed(missing):
            resolved /= part
        if (
            resolved == workspace
            or resolved.is_relative_to(workspace)
            or workspace.is_relative_to(resolved)
        ):
            raise KernelError("product_state_overlap", "产品状态目录不能与Workspace互相包含")
        if (
            not missing
            and os.name == "posix"
            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise OSError
    except KernelError:
        raise
    except (OSError, RuntimeError):
        raise KernelError("product_state_invalid", "产品状态目录权限或身份无效") from None


def _git_binding(path: Path, platform: PreflightPlatform) -> None:
    if platform == "windows":
        raise KernelError(
            "product_git_platform_unsupported",
            "Windows原生Git读取尚未开放",
        )
    try:
        executable = path.resolve(strict=True)
        info = executable.stat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError
    except (OSError, RuntimeError):
        raise KernelError("product_git_invalid", "Git可执行文件不可用") from None


def run_product_preflight(
    request: ProductPreflightRequest,
    *,
    dependency_finder: DependencyFinder = importlib.util.find_spec,
    environment: Mapping[str, str] | None = None,
    workspace_probe: WorkspaceProbe = _default_workspace_probe,
    platform_name: str | None = None,
    monotonic_clock: MonotonicClock = time.monotonic,
) -> ProductPreflightReport:
    """执行离线、只读检查；预期失败转为稳定Check并继续独立检查。"""

    platform = _platform(platform_name)
    checks: dict[str, ProductPreflightCheck] = {}

    def started() -> float:
        try:
            return monotonic_clock()
        except Exception:
            return 0.0

    def record(
        check_id: str,
        category: PreflightCategory,
        requirement: PreflightRequirement,
        status: PreflightStatus,
        code: str,
        remediation_id: str | None,
        began: float,
    ) -> None:
        checks[check_id] = _check(
            check_id=check_id,
            category=category,
            requirement=requirement,
            status=status,
            code=code,
            remediation_id=remediation_id,
            started=began,
            clock=monotonic_clock,
        )

    body: bytes | None = None
    snapshot: ProductConfigSnapshot | None = None
    selection = None
    configuration: ConfigurationDiagnosticReport | None = None
    configuration_internal_failure = False

    began = started()
    try:
        body = read_product_config_bytes(request.config_path)
        record(
            "product_config_file",
            "config",
            "required",
            "passed",
            "product_config_file_ready",
            None,
            began,
        )
    except KernelError as error:
        record(
            "product_config_file",
            "config",
            "required",
            "failed",
            error.code,
            "run_configure",
            began,
        )
    except Exception:
        record(
            "product_config_file",
            "config",
            "required",
            "failed",
            "product_internal_failure",
            "run_configure",
            began,
        )

    began = started()
    if body is None:
        record(
            "product_config_contract",
            "config",
            "required",
            "skipped",
            "product_config_prerequisite_failed",
            "run_configure",
            began,
        )
    else:
        try:
            document = decode_product_config_bytes(body, allow_legacy=True)
            if isinstance(document, ProductConfigV1):
                raise KernelError(
                    "product_config_migration_required",
                    "产品配置必须先迁移到v2",
                )
            snapshot = ProductConfigSnapshot(
                source_sha256=hashlib.sha256(body).hexdigest(),
                config_sha256=product_config_digest(document),
                loaded_at=utc_now(),
                config=document,
            )
            record(
                "product_config_contract",
                "config",
                "required",
                "passed",
                "product_config_contract_ready",
                None,
                began,
            )
        except KernelError as error:
            record(
                "product_config_contract",
                "config",
                "required",
                "failed",
                error.code,
                "migrate_or_configure",
                began,
            )
        except Exception:
            record(
                "product_config_contract",
                "config",
                "required",
                "failed",
                "product_internal_failure",
                "migrate_or_configure",
                began,
            )

    began = started()
    if snapshot is None:
        record(
            "product_profile_selection",
            "profile",
            "required",
            "skipped",
            "product_config_prerequisite_failed",
            "select_profile",
            began,
        )
    else:
        try:
            selection = select_profile(snapshot, request.profile_id)
            record(
                "product_profile_selection",
                "profile",
                "required",
                "passed",
                "product_profile_ready",
                None,
                began,
            )
        except KernelError as error:
            record(
                "product_profile_selection",
                "profile",
                "required",
                "failed",
                error.code,
                "select_profile",
                began,
            )
        except Exception:
            record(
                "product_profile_selection",
                "profile",
                "required",
                "failed",
                "product_internal_failure",
                "select_profile",
                began,
            )

    if snapshot is not None and selection is not None:
        try:
            configuration = diagnose_configuration(
                snapshot,
                selection,
                environment_secret_provider(snapshot.config, environment=environment),
                dependency_finder=dependency_finder,
            )
        except Exception:
            configuration = None
            configuration_internal_failure = True

    for check_id, category, scopes, failure_code, remediation_id in (
        (
            "product_provider_dependencies",
            "dependency",
            frozenset({"dependency"}),
            "product_dependency_missing",
            "install_provider_extra",
        ),
        (
            "product_provider_secrets",
            "secret",
            frozenset({"secret", "provider"}),
            "secret_unavailable",
            "set_environment_secret",
        ),
    ):
        began = started()
        if configuration_internal_failure:
            record(
                check_id,
                cast(PreflightCategory, category),
                "required",
                "failed",
                "product_internal_failure",
                remediation_id,
                began,
            )
            continue
        if configuration is None:
            record(
                check_id,
                cast(PreflightCategory, category),
                "required",
                "skipped",
                "product_config_prerequisite_failed",
                remediation_id,
                began,
            )
            continue
        selected_checks = [item for item in configuration.checks if item.scope in scopes]
        passed = bool(selected_checks) and all(item.status == "passed" for item in selected_checks)
        record(
            check_id,
            cast(PreflightCategory, category),
            "required",
            "passed" if passed else "failed",
            f"{check_id}_ready" if passed else failure_code,
            None if passed else remediation_id,
            began,
        )

    began = started()
    platform_ready = platform == "windows" or (os.name == "posix" and hasattr(os, "O_NOFOLLOW"))
    if platform_name is not None:
        platform_ready = platform == "windows" or platform == "posix"
    record(
        "product_platform_read_port",
        "platform",
        "required",
        "passed" if platform_ready else "failed",
        "product_platform_read_port_ready"
        if platform_ready
        else "product_tools_platform_unsupported",
        None if platform_ready else "use_supported_platform",
        began,
    )

    workspace_root: Path | None = None
    began = started()
    try:
        workspace_root = workspace_probe(request.workspace, platform)
        if snapshot is not None:
            config_file = request.config_path.resolve(strict=True)
            if config_file.is_relative_to(workspace_root):
                raise KernelError("product_config_overlap", "产品配置文件不能位于Workspace内")
        record(
            "product_workspace_binding",
            "workspace",
            "required",
            "passed",
            "product_workspace_ready",
            None,
            began,
        )
    except KernelError as error:
        record(
            "product_workspace_binding",
            "workspace",
            "required",
            "failed",
            error.code,
            "inspect_workspace",
            began,
        )
    except Exception:
        record(
            "product_workspace_binding",
            "workspace",
            "required",
            "failed",
            "product_internal_failure",
            "inspect_workspace",
            began,
        )

    began = started()
    if workspace_root is None:
        record(
            "product_state_layout",
            "state",
            "required",
            "skipped",
            "product_workspace_prerequisite_failed",
            "choose_state_directory",
            began,
        )
    else:
        try:
            _state_layout(request.state_directory, workspace_root)
            record(
                "product_state_layout",
                "state",
                "required",
                "passed",
                "product_state_layout_ready",
                None,
                began,
            )
        except KernelError as error:
            record(
                "product_state_layout",
                "state",
                "required",
                "failed",
                error.code,
                "choose_state_directory",
                began,
            )
        except Exception:
            record(
                "product_state_layout",
                "state",
                "required",
                "failed",
                "product_internal_failure",
                "choose_state_directory",
                began,
            )

    began = started()
    tui_requirement: PreflightRequirement = "required" if request.require_tui else "advisory"
    tui_code = "product_tui_dependency_ready"
    try:
        tui_present = dependency_finder("textual") is not None
        if not tui_present:
            tui_code = "tui_dependency_missing"
    except (ImportError, AttributeError, ValueError):
        tui_present = False
        tui_code = "tui_dependency_missing"
    except Exception:
        tui_present = False
        tui_code = "product_internal_failure"
    tui_status: PreflightStatus = "passed" if tui_present else "failed"
    if not request.require_tui and not tui_present:
        tui_status = "skipped"
    record(
        "product_tui_dependency",
        "tui",
        tui_requirement,
        tui_status,
        tui_code,
        None if tui_present else "install_tui_extra",
        began,
    )

    began = started()
    if request.git_executable is None:
        record(
            "product_git_binding",
            "git",
            "advisory",
            "skipped",
            "product_git_not_configured",
            None,
            began,
        )
    else:
        try:
            _git_binding(request.git_executable, platform)
            record(
                "product_git_binding",
                "git",
                "required",
                "passed",
                "product_git_binding_ready",
                None,
                began,
            )
        except KernelError as error:
            record(
                "product_git_binding",
                "git",
                "required",
                "failed",
                error.code,
                "omit_or_install_git",
                began,
            )
        except Exception:
            record(
                "product_git_binding",
                "git",
                "required",
                "failed",
                "product_internal_failure",
                "omit_or_install_git",
                began,
            )

    ordered = tuple(checks[key] for key in sorted(checks))
    ready = (
        configuration is not None
        and configuration.ready
        and all(item.status == "passed" for item in ordered if item.requirement == "required")
    )
    candidate = ProductPreflightReport.model_construct(
        mode=request.mode,
        platform=platform,
        workspace_fingerprint=_workspace_fingerprint(request.workspace),
        config_sha256=configuration.config_sha256 if configuration is not None else None,
        selected_profile=configuration.selected_profile if configuration is not None else None,
        configuration=configuration,
        checks=ordered,
        ready=ready,
        generated_at=utc_now(),
        report_sha256="0" * 64,
    )
    return ProductPreflightReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=product_preflight_report_digest(candidate),
    )
