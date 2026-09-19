"""Eval执行器共用的私有目录与单写者文件锁。"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.domain.file_lock import acquire_exclusive_file_lock


def ensure_private_directory(path: Path, *, error_code: str, label: str) -> None:
    """创建或验证0700目录，拒绝符号链接和权限漂移。"""

    try:
        if path.is_symlink():
            raise OSError
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError
    except OSError:
        raise KernelError(error_code, f"{label}缺失、权限错误或不安全") from None


def path_present(path: Path) -> bool:
    """同时识别普通路径和悬空符号链接。"""

    return path.exists() or path.is_symlink()


@contextmanager
def exclusive_execution_lock(
    root: Path,
    filename: str,
    *,
    busy_code: str,
    invalid_code: str,
    label: str,
) -> Iterator[None]:
    """以0600普通文件取得跨平台非阻塞独占锁。"""

    descriptor: int | None = None
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_path = root / filename
        if lock_path.is_symlink():
            raise OSError
        descriptor = os.open(lock_path, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError
        try:
            acquire_exclusive_file_lock(descriptor)
        except BlockingIOError:
            raise KernelError(busy_code, f"{label}已有活跃执行宿主") from None
        yield
    except KernelError:
        raise
    except OSError:
        raise KernelError(invalid_code, f"{label}执行锁不可用") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
