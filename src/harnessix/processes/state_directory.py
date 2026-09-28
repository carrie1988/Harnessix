"""Process状态根与Run目录准备；双平台安全边界保持独立，拒绝既有非私有Windows对象。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from harnessix.agent.errors import KernelError


def _safe_state_root(value: str | Path) -> Path:
    path = Path(value)
    try:
        if os.name == "nt":
            from harnessix.workspace.windows_private_directory import private_state_directory

            private_state_directory(path, parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError
        if os.name == "posix":
            path.chmod(0o700)
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
    except (OSError, ValueError, KernelError):
        raise KernelError("process_state_invalid", "Process状态目录无效") from None
    return path


def _create_run_directory(path: Path) -> None:
    """新的Process Run不得复用既有目录；Windows沿用私有可继承状态契约。"""
    if os.name == "nt":
        from harnessix.workspace.windows_private_directory import private_state_directory

        private_state_directory(path)
    else:
        path.mkdir(mode=0o700)
