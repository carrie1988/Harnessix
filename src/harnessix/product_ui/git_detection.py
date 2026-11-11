"""用户入口的Git探测：未显式指定时找到可运行的git，探测失败按未配置处理。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.preflight_environment import validate_windows_git


def _git_runs(path: str) -> bool:
    """确认git可运行；未装命令行工具的macOS上/usr/bin/git是会弹安装窗口的占位程序，先排除。"""

    try:
        if sys.platform == "darwin" and path == "/usr/bin/git":
            probe = subprocess.run(
                ["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=5, check=False
            )
            if probe.returncode != 0:
                return False
        return (
            subprocess.run([path, "--version"], capture_output=True, timeout=5, check=False)
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def bindable_windows_git(found: Path, bindable: Callable[[Path], object]) -> Path | None:
    """Windows上PATH里的git换成同一安装中能通过产品绑定校验的git.exe。

    Git for Windows默认安装里，PATH上的cmd\\git.exe与git-lfs.exe、mingw64\\bin\\git.exe与
    git-upload-pack.exe等互为硬链接；产品Git绑定拒绝多硬链接EXE，用户装好就无法启动
    （2026-11-11 Windows 11实机，Git 2.53）。同一安装里的bin\\git.exe是单链接启动器，可正常使用。
    """

    roots = (found.parent.parent, found.parent.parent.parent)
    candidates = (found, *(root / "bin" / "git.exe" for root in roots))
    candidates += tuple(root / "mingw64" / "bin" / "git.exe" for root in roots)
    for candidate in dict.fromkeys(candidates):
        try:
            bindable(candidate)
        except KernelError:
            continue
        return candidate
    return None


def detected_git_executable(explicit: str | None, workspace: Path) -> str | None:
    """显式路径优先；否则工作区在Git仓库内时用PATH上可运行的git，探测失败按未配置处理。"""

    if explicit is not None:
        return explicit
    if not any((path / ".git").exists() for path in (workspace, *workspace.parents)):
        return None
    found = shutil.which("git")
    if found is not None and os.name == "nt":
        chosen = bindable_windows_git(Path(found), validate_windows_git)
        found = None if chosen is None else found if chosen == Path(found) else str(chosen)
    return found if found is not None and _git_runs(found) else None
