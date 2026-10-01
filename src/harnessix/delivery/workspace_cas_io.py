"""原 Workspace CAS 的完整文件 IO；由 Store 先验证摘要、写权限和固定私有路径。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES


def read_blob_body(path: Path, digest: str) -> bytes:
    """完整回读原私有普通文件并核对 SHA；调用者已验证固定摘要地址。"""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_TRANSACTION_FILE_BYTES:
            raise OSError
        if os.name == "posix" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError
        body = bytearray()
        while len(body) <= MAX_TRANSACTION_FILE_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_TRANSACTION_FILE_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        result = bytes(body)
        if len(result) != info.st_size or hashlib.sha256(result).hexdigest() != digest:
            raise OSError
        return result
    except OSError:
        raise KernelError("delivery_blob_corrupt", "Workspace事务Blob损坏或缺失") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def confirm_blob_durable(
    path: Path,
    body: bytes,
    read_body: Callable[[], bytes],
    sync_directory: Callable[[Path], None],
) -> None:
    """确认已有完整正文的文件与原目录耐久；不替换、授权或删除业务对象。"""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        # Windows FlushFileBuffers 需要写权限；路径仍为原固定摘要和私有 CAS。
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(body):
            raise OSError
        os.fsync(descriptor)
        sync_directory(path.parent)
        if read_body() != body:
            raise OSError
    except OSError:
        raise KernelError("delivery_storage_unavailable", "Workspace事务Blob耐久确认失败") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
