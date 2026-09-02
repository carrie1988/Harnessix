from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from harnessix.agent.errors import KernelError


@contextmanager
def storage_errors() -> Iterator[None]:
    """将驱动和文件系统失败收敛为稳定错误，不向上暴露路径、SQL 或原始异常文本。"""
    try:
        yield
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            failure = KernelError(
                "storage_busy", "Session 存储忙，请按原请求 ID 重试", retryable=True
            )
        elif code == sqlite3.SQLITE_FULL:
            failure = KernelError("storage_full", "Session 存储空间不足")
        elif code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            failure = KernelError("database_corrupt", "Session 数据库损坏或格式错误")
        else:
            failure = KernelError("storage_unavailable", "Session 存储操作失败")
        raise failure from None
    except OSError:
        raise KernelError("storage_unavailable", "Session 文件系统操作失败") from None
