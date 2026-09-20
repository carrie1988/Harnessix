"""产品Action Runtime宿主锁：在打开任一Store或Process Owner前取得。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.domain.file_lock import acquire_exclusive_file_lock


@contextmanager
def product_action_runtime_lock(state_root: Path) -> Iterator[None]:
    """跨平台持有整个Action Runtime生命周期，进程退出时由OS自动释放。"""

    path = state_root / "product-action-runtime.lock"
    descriptor = -1
    try:
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        acquire_exclusive_file_lock(descriptor)
    except BlockingIOError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise KernelError("action_runtime_busy", "产品Action Runtime已有活跃宿主") from error
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise KernelError("action_runtime_owner_unavailable", "产品Action宿主锁不可用") from None
    try:
        yield
    finally:
        os.close(descriptor)
