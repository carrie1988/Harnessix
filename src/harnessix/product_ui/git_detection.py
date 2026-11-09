"""用户入口的Git探测：未显式指定时找到可运行的git，探测失败按未配置处理。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


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


def detected_git_executable(explicit: str | None, workspace: Path) -> str | None:
    """显式路径优先；否则工作区在Git仓库内时用PATH上可运行的git，探测失败按未配置处理。"""

    if explicit is not None:
        return explicit
    if not any((path / ".git").exists() for path in (workspace, *workspace.parents)):
        return None
    found = shutil.which("git")
    return found if found is not None and _git_runs(found) else None
