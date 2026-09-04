"""副本账本 v1 → v2；调用者持有副本锁并先校验旧数据。"""

import sqlite3

SCHEMA_VERSION = 2
BATCH_DDL = (
    "CREATE TABLE batches (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
    "payload TEXT NOT NULL, checksum TEXT NOT NULL)",
    "CREATE TABLE batch_approvals (batch_id TEXT PRIMARY KEY REFERENCES batches(id), "
    "payload TEXT NOT NULL, checksum TEXT NOT NULL)",
    "ALTER TABLE plans ADD COLUMN owner_batch_id TEXT REFERENCES batches(id)",
    "CREATE INDEX plans_owner_batch ON plans(owner_batch_id)",
)


def _fault(point: str) -> None:
    """真实进程退出测试切点。"""


def add_batches(db: sqlite3.Connection) -> None:
    # 不使用 executescript：它会隐式提交调用者的事务。
    for statement in BATCH_DDL:
        db.execute(statement)
    _fault("migration_before_version")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _fault("migration_before_commit")
