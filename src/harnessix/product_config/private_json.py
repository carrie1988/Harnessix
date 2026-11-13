"""用户配置目录里的小型私有JSON：只允许当前用户读写，临时文件加原子替换写入。

“读出→修改→写回”要放在`serialized_update`里：几个Harnessix进程同时改同一份文件时（例如同时在几个新仓库
确认信任），没有串行的话后写的会覆盖先写的，先确认的记录丢失（Beta Issue #15，三个平台都会发生）。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from harnessix.file_lock import acquire_exclusive_file_lock, release_exclusive_file_lock

# 持锁的只是一次读写（毫秒级）；等这么久仍拿不到，说明另一个进程卡住了。
LOCK_WAIT_SECONDS: Final = 10.0
# Windows上目标文件正被其他进程读取时替换会被拒绝（WinError 5），读取很快结束，稍后重试即可。
# POSIX的rename不受打开的读者影响，那里的PermissionError是真的没有权限，直接报出。
REPLACE_WAIT_SECONDS: Final = 2.0
_RETRY_INTERVAL: Final = 0.02
_RETRY_REPLACE_ON_PERMISSION_ERROR = os.name == "nt"


class PrivateJsonBusy(OSError):
    """等待上限内没有取得这份文件的更新锁。"""


@contextmanager
def serialized_update(path: Path) -> Iterator[None]:
    """同一份私有JSON的“读出→修改→写回”跨进程串行；锁在同目录的`<文件名>.lock`上。"""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.with_name(f"{path.name}.lock"), flags, 0o600)
    try:
        _wait_for_lock(descriptor)
        try:
            yield
        finally:
            release_exclusive_file_lock(descriptor)
    finally:
        os.close(descriptor)


def write_private_json(path: Path, document: object) -> None:
    """父目录按0700创建；写入失败时不留下临时文件，原文件保持不变。"""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    body = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body + "\n")
        _replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _wait_for_lock(descriptor: int) -> None:
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            acquire_exclusive_file_lock(descriptor)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise PrivateJsonBusy("另一个Harnessix进程正在更新这份记录") from None
            time.sleep(_RETRY_INTERVAL)


def _replace(source: Path, target: Path) -> None:
    deadline = time.monotonic() + REPLACE_WAIT_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not _RETRY_REPLACE_ON_PERMISSION_ERROR or time.monotonic() >= deadline:
                raise
            time.sleep(_RETRY_INTERVAL)
