"""已有SQLite领域Reader的只读连接；不创建数据库、不迁移、不补写任何证明。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def readonly_database(path: Path) -> sqlite3.Connection:
    """保留真实WAL读取语义；query_only与mode=ro同时禁止领域Writer写入。"""
    database = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        database.execute("PRAGMA query_only = ON")
        database.execute("PRAGMA foreign_keys = ON")
        return database
    except BaseException:
        database.close()
        raise
