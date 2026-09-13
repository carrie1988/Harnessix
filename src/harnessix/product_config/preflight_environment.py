"""产品预检的宿主平台、Workspace、状态目录、TUI与Git检查。"""

from __future__ import annotations

import importlib.util
import os
import stat
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.preflight_support import PreflightRecorder
from harnessix.product_config.product_contracts import PreflightPlatform, PreflightStatus
from harnessix.product_config.runtime import DependencyFinder
from harnessix.workspace.snapshot import SecureWorkspaceReader

WorkspaceProbe = Callable[[Path, PreflightPlatform], Path]


def default_workspace_probe(path: Path, platform: PreflightPlatform) -> Path:
    """通过平台安全端口绑定工作区，仅返回规范根路径。"""

    with SecureWorkspaceReader(path, platform=platform) as reader:
        return reader.path


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _state_layout(path: Path, workspace: Path) -> None:
    candidate = Path(os.path.abspath(path))
    if _overlaps(candidate, workspace):
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
        if _overlaps(resolved, workspace):
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
        raise KernelError("product_git_platform_unsupported", "Windows原生Git读取尚未开放")
    try:
        executable = path.resolve(strict=True)
        if not stat.S_ISREG(executable.stat().st_mode):
            raise OSError
    except (OSError, RuntimeError):
        raise KernelError("product_git_invalid", "Git可执行文件不可用") from None


def _check_platform(
    platform: PreflightPlatform,
    platform_name: str | None,
    recorder: PreflightRecorder,
) -> None:
    began = recorder.started()
    ready = platform == "windows" or (os.name == "posix" and hasattr(os, "O_NOFOLLOW"))
    if platform_name is not None:
        ready = platform in {"windows", "posix"}
    recorder.record(
        "product_platform_read_port",
        "platform",
        "required",
        "passed" if ready else "failed",
        "product_platform_read_port_ready" if ready else "product_tools_platform_unsupported",
        None if ready else "use_supported_platform",
        began,
    )


def _check_workspace(
    workspace: Path,
    config_path: Path,
    config_loaded: bool,
    platform: PreflightPlatform,
    probe: WorkspaceProbe,
    recorder: PreflightRecorder,
) -> Path | None:
    began = recorder.started()
    root: Path | None = None
    try:
        root = probe(workspace, platform)
        if config_loaded and config_path.resolve(strict=True).is_relative_to(root):
            raise KernelError("product_config_overlap", "产品配置文件不能位于Workspace内")
        recorder.record(
            "product_workspace_binding",
            "workspace",
            "required",
            "passed",
            "product_workspace_ready",
            None,
            began,
        )
        return root
    except KernelError as error:
        recorder.record(
            "product_workspace_binding",
            "workspace",
            "required",
            "failed",
            error.code,
            "inspect_workspace",
            began,
        )
    except Exception:
        recorder.record(
            "product_workspace_binding",
            "workspace",
            "required",
            "failed",
            "product_internal_failure",
            "inspect_workspace",
            began,
        )
    return root


def _check_state(path: Path, workspace: Path | None, recorder: PreflightRecorder) -> None:
    began = recorder.started()
    if workspace is None:
        recorder.record(
            "product_state_layout",
            "state",
            "required",
            "skipped",
            "product_workspace_prerequisite_failed",
            "choose_state_directory",
            began,
        )
        return
    try:
        _state_layout(path, workspace)
        recorder.record(
            "product_state_layout",
            "state",
            "required",
            "passed",
            "product_state_layout_ready",
            None,
            began,
        )
    except KernelError as error:
        recorder.record(
            "product_state_layout",
            "state",
            "required",
            "failed",
            error.code,
            "choose_state_directory",
            began,
        )
    except Exception:
        recorder.record(
            "product_state_layout",
            "state",
            "required",
            "failed",
            "product_internal_failure",
            "choose_state_directory",
            began,
        )


def _check_tui(
    required: bool,
    dependency_finder: DependencyFinder,
    recorder: PreflightRecorder,
) -> None:
    began = recorder.started()
    code = "product_tui_dependency_ready"
    try:
        present = dependency_finder("textual") is not None
        if not present:
            code = "tui_dependency_missing"
    except (ImportError, AttributeError, ValueError):
        present, code = False, "tui_dependency_missing"
    except Exception:
        present, code = False, "product_internal_failure"
    status: PreflightStatus = "passed" if present else "failed"
    if not required and not present:
        status = "skipped"
    recorder.record(
        "product_tui_dependency",
        "tui",
        "required" if required else "advisory",
        status,
        code,
        None if present else "install_tui_extra",
        began,
    )


def _check_git(
    executable: Path | None,
    platform: PreflightPlatform,
    recorder: PreflightRecorder,
) -> None:
    began = recorder.started()
    if executable is None:
        recorder.record(
            "product_git_binding",
            "git",
            "advisory",
            "skipped",
            "product_git_not_configured",
            None,
            began,
        )
        return
    try:
        _git_binding(executable, platform)
        recorder.record(
            "product_git_binding",
            "git",
            "required",
            "passed",
            "product_git_binding_ready",
            None,
            began,
        )
    except KernelError as error:
        recorder.record(
            "product_git_binding",
            "git",
            "required",
            "failed",
            error.code,
            "omit_or_install_git",
            began,
        )
    except Exception:
        recorder.record(
            "product_git_binding",
            "git",
            "required",
            "failed",
            "product_internal_failure",
            "omit_or_install_git",
            began,
        )


def inspect_environment(
    *,
    workspace: Path,
    config_path: Path,
    config_loaded: bool,
    state_directory: Path,
    git_executable: Path | None,
    require_tui: bool,
    platform: PreflightPlatform,
    platform_name: str | None,
    workspace_probe: WorkspaceProbe = default_workspace_probe,
    dependency_finder: DependencyFinder = importlib.util.find_spec,
    recorder: PreflightRecorder,
) -> None:
    """执行彼此独立的本地运行环境检查，预期失败只写入事实。"""

    _check_platform(platform, platform_name, recorder)
    root = _check_workspace(
        workspace, config_path, config_loaded, platform, workspace_probe, recorder
    )
    _check_state(state_directory, root, recorder)
    _check_tui(require_tui, dependency_finder, recorder)
    _check_git(git_executable, platform, recorder)
