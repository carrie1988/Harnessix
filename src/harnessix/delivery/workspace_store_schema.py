"""Workspace数据库的格式准入与元数据升级；不编解码或重写领域记录。"""

from __future__ import annotations

import sqlite3

from harnessix.agent.errors import KernelError

_SCHEMA_VERSION = "2"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1", _SCHEMA_VERSION})


def initialize_workspace_store(database: sqlite3.Connection) -> None:
    """初始化Schema2或原子升级Schema1准入，不重写任何历史记录。"""
    database.execute(
        "CREATE TABLE IF NOT EXISTS delivery_metadata "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
    )
    row = database.execute(
        "SELECT value FROM delivery_metadata WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        database.execute(
            "INSERT INTO delivery_metadata VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
    elif row[0] not in SUPPORTED_SCHEMA_VERSIONS:
        raise KernelError("delivery_store_version", "Workspace事务存储版本不受支持")
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS workspace_transactions (
            transaction_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            plan_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            payload TEXT NOT NULL
        ) STRICT;
        CREATE INDEX IF NOT EXISTS workspace_transactions_state
            ON workspace_transactions(state);
        CREATE TABLE IF NOT EXISTS workspace_transaction_events (
            transaction_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            state TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(transaction_id, sequence),
            FOREIGN KEY(transaction_id) REFERENCES workspace_transactions(transaction_id)
        ) STRICT;
        """
    )
    if row == ("1",):
        _upgrade_schema(database)


def check_workspace_store_schema(database: sqlite3.Connection) -> None:
    """只读打开也验证版本，不能等待载入首条记录才发现未知数据库。"""
    try:
        row = database.execute(
            "SELECT value FROM delivery_metadata WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error:
        raise KernelError("delivery_store_version", "Workspace事务存储版本不受支持") from None
    if row is None or row[0] not in SUPPORTED_SCHEMA_VERSIONS:
        raise KernelError("delivery_store_version", "Workspace事务存储版本不受支持")


def _upgrade_schema(database: sqlite3.Connection) -> None:
    """独占事务仅升级格式准入；不转换原JSON、事件、摘要或批准。"""
    try:
        database.execute("BEGIN IMMEDIATE")
        check_workspace_store_schema(database)
        database.execute(
            "UPDATE delivery_metadata SET value=? WHERE key='schema_version' AND value='1'",
            (_SCHEMA_VERSION,),
        )
        database.execute("COMMIT")
    except BaseException:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise
