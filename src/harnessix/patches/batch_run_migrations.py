"""v2 → v3 的独立迁移；保留原 v1 → v2 实现。"""

import sqlite3

SCHEMA_VERSION = 3


def _fault(point: str) -> None:
    """真实迁移崩溃切点。"""


def add_runs(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE batch_run_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "batch_id TEXT NOT NULL REFERENCES batches(id), "
        "phase TEXT NOT NULL CHECK(phase IN ('started','finished')), "
        "payload TEXT NOT NULL, checksum TEXT NOT NULL, UNIQUE(batch_id,phase))"
    )
    _fault("runs_before_version")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _fault("runs_before_commit")
