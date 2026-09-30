"""Git账本原v1结构的唯一初始化与只读验证；不读取或重签业务事件。"""

from __future__ import annotations

import sqlite3

from harnessix.agent.errors import KernelError

_SCHEMA_VERSION = "1"
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS git_delivery_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS git_worktrees (
    worktree_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL UNIQUE,
    plan_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS git_worktree_events (
    worktree_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(worktree_id, sequence),
    FOREIGN KEY(worktree_id) REFERENCES git_worktrees(worktree_id)
) STRICT;
CREATE TABLE IF NOT EXISTS git_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    worktree_id TEXT NOT NULL UNIQUE,
    transaction_id TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS git_commits (
    commit_id TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL UNIQUE,
    branch_ref TEXT NOT NULL UNIQUE,
    spec_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS git_commit_events (
    commit_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(commit_id, sequence),
    FOREIGN KEY(commit_id) REFERENCES git_commits(commit_id)
) STRICT;
"""


def initialize_git_store(database: sqlite3.Connection) -> None:
    """Writer沿用原DDL及版本插入规则；不迁移已有其他版本。"""
    database.executescript(_SCHEMA_SQL)
    row = database.execute(
        "SELECT value FROM git_delivery_metadata WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        database.execute(
            "INSERT INTO git_delivery_metadata VALUES ('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
    elif row != (_SCHEMA_VERSION,):
        raise KernelError("git_delivery_store_version", "Git交付存储版本不受支持")


def verify_git_store_schema(database: sqlite3.Connection) -> None:
    """Reader只接受原v1六表定义；缺失或变形不会触发补建。"""
    try:
        row = database.execute(
            "SELECT value FROM git_delivery_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise KernelError("git_delivery_store_corrupt", "Git交付账本版本记录缺失")
        if row != (_SCHEMA_VERSION,):
            raise KernelError("git_delivery_store_version", "Git交付存储版本不受支持")
        expected = {
            sql.split()[5]: " ".join(sql.replace("IF NOT EXISTS ", "", 1).split())
            for sql in _SCHEMA_SQL.split(";")
            if sql.strip()
        }
        actual = {
            name: " ".join(sql.split())
            for name, sql in database.execute(
                "SELECT name, sql FROM sqlite_schema "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' LIMIT 7"
            )
        }
        if actual != expected:
            raise KernelError("git_delivery_store_corrupt", "Git交付账本结构不一致")
    except sqlite3.Error:
        raise KernelError("git_delivery_store_corrupt", "Git交付账本结构无效") from None
