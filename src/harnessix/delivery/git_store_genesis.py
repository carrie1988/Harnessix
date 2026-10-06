"""仅初始化空Git账本的v2结构；事务与MAC创世认证均由调用者负责。"""

from __future__ import annotations

import sqlite3

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store_schema import _SCHEMA_SQL, _SCHEMA_VERSION
from harnessix.delivery.git_store_schema_v2 import (
    _V1_SCHEMA,
    _create_git_store_v2_tables,
    _observe,
    _statements,
    verify_git_store_v2_schema,
)

_V1_BUSINESS_TABLES = (
    "git_worktrees",
    "git_worktree_events",
    "git_checkpoints",
    "git_commits",
    "git_commit_events",
)


def _corrupt() -> KernelError:
    return KernelError("git_delivery_store_corrupt", "Git交付账本结构或版本记录不一致")


def _read_only() -> KernelError:
    return KernelError("git_delivery_store_read_only", "Git交付只读账本不接受初始化")


def _initialize_empty_v1(database: sqlite3.Connection) -> None:
    """复用原v1唯一DDL及版本值，逐句执行以保留调用者事务。"""
    if database.execute(
        "SELECT 1 FROM temp.sqlite_schema LIMIT 1"
    ).fetchone() is not None or database.execute("PRAGMA main.encoding").fetchone() != ("UTF-8",):
        raise _corrupt()
    # 原initialize_git_store使用executescript且没有公开的逐句入口，不能直接调用。
    for statement in _statements(_SCHEMA_SQL):
        database.execute(statement)
    database.execute(
        "INSERT INTO main.git_delivery_metadata VALUES ('schema_version', ?)",
        (_SCHEMA_VERSION,),
    )


def _require_empty_v1(database: sqlite3.Connection) -> None:
    """只检查记录是否存在，不解析、重签或迁移任何旧业务记录。"""
    for table in _V1_BUSINESS_TABLES:
        if database.execute(f"SELECT 1 FROM main.{table} LIMIT 1").fetchone() is not None:
            raise KernelError(
                "git_delivery_store_legacy_unproven", "已有Git交付业务记录不能补签升代"
            )
    if database.execute("SELECT key,value FROM main.git_delivery_metadata LIMIT 2").fetchall() != [
        ("schema_version", _SCHEMA_VERSION)
    ]:
        raise _corrupt()


def _require_writable(database: sqlite3.Connection) -> None:
    """在已核验的真实表上验证写权限，不修改任何行或生成认证记录。"""
    # URI mode=ro不反映在query_only中；零行UPDATE仍校验底层写权限。
    database.execute("UPDATE main.git_delivery_metadata SET value=value WHERE 0")


def initialize_git_store_v2(database: sqlite3.Connection) -> None:
    """在调用者事务内初始化空账本，或重复核验完整v2。

    仅接受全新空结构、原v1六表且仅有一行版本元数据、完整版本2结构。
    拒绝只读连接、未知或部分结构以及任何已有v1业务记录。
    不开启、提交或回滚事务；失败后的事务处理仍由调用者负责。
    只创建结构并更新版本，不填充MAC创世锚点或发布记录。
    """
    if not database.in_transaction:
        raise KernelError(
            "git_delivery_store_transaction_required", "Git结构初始化需要调用者已有事务"
        )
    try:
        if database.execute("PRAGMA query_only").fetchone() == (1,):
            raise _read_only()
        if database.execute("SELECT 1 FROM main.sqlite_schema LIMIT 1").fetchone() is None:
            _initialize_empty_v1(database)
        version, actual = _observe(database)
        if version == "2":
            verify_git_store_v2_schema(database)
            _require_writable(database)
            return
        if actual != _V1_SCHEMA:
            raise _corrupt()
        _require_empty_v1(database)
        _require_writable(database)
        _create_git_store_v2_tables(database)
        database.execute(
            "UPDATE main.git_delivery_metadata SET value='2' WHERE key='schema_version'"
        )
        verify_git_store_v2_schema(database)
    except sqlite3.Error as error:
        code = getattr(error, "sqlite_errorcode", None)
        if code is not None and code & 0xFF == sqlite3.SQLITE_READONLY:
            raise _read_only() from None
        raise _corrupt() from None
