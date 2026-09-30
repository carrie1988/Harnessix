"""Git 固定绑定的物理身份：复用原路径、目录与可执行文件观察，不执行命令。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest


def _path_text(path: Path) -> str:
    value = str(path)
    return os.path.normcase(value) if os.name == "nt" else value


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(_path_text(path).encode("utf-8")).hexdigest()


def _identity(path: Path, *, directory: bool) -> str:
    try:
        info = path.lstat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid_type or stat.S_ISLNK(info.st_mode):
            raise OSError
        return canonical_digest(
            {
                "path": _path_text(path.resolve(strict=True)),
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode_type": stat.S_IFMT(info.st_mode),
            }
        )
    except OSError:
        raise KernelError("git_binding_changed", "Git绑定对象身份无效") from None


def _executable_identity(path: Path) -> str:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            raise OSError
        return canonical_digest(
            {
                "path": _path_text(path.resolve(strict=True)),
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "mode": info.st_mode,
            }
        )
    except OSError:
        raise KernelError("git_executable_invalid", "Git可执行文件身份无效") from None
