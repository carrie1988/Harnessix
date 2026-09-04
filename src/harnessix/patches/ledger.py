"""每个受管副本独立的私有账本，不改变 Agent Session 数据库。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from harnessix.patches.batch_run_migrations import SCHEMA_VERSION as SCHEMA_VERSION
from harnessix.patches.batch_run_migrations import add_runs
from harnessix.patches.contracts import PatchProposal, PreparedPatch
from harnessix.patches.ledger_migrations import add_batches
from harnessix.patches.managed_contracts import MAX_COPY_PLANS, MAX_PLAN_BYTES, PatchRecord
from harnessix.patches.managed_io import fail
from harnessix.patches.planner import validate_prepared
from harnessix.tools.workspace import ReadOperation, Workspace, digest

APPLICATION_ID = 0x48585057


def initialize(db: sqlite3.Connection, metadata: dict[str, object]) -> None:
    db.executescript(f"""
        PRAGMA application_id={APPLICATION_ID};
        PRAGMA user_version=1;
        CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        CREATE TABLE baseline (path TEXT PRIMARY KEY, body BLOB NOT NULL);
        CREATE TABLE plans (
            id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
            proposal TEXT NOT NULL, before_image BLOB NOT NULL, after_image BLOB NOT NULL
        );
        CREATE TABLE events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL REFERENCES plans(id), payload TEXT NOT NULL,
            temporary TEXT, checksum TEXT NOT NULL
        );
    """)
    db.execute("INSERT INTO metadata VALUES (1, ?)", (json.dumps(metadata),))
    with transaction(db):
        add_batches(db)
        add_runs(db)


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def fingerprint(record: PatchRecord) -> str:
    return digest(
        (
            str(record.workspace_id),
            str(record.plan_id),
            record.request_id,
            record.manifest.fingerprint,
        )
    )


def append(db: sqlite3.Connection, record: PatchRecord, temporary: tuple[int, int] | None) -> None:
    payload = record.model_dump_json()
    evidence = json.dumps(temporary) if temporary is not None else None
    db.execute(
        "INSERT INTO events(plan_id,payload,temporary,checksum) VALUES(?,?,?,?)",
        (str(record.plan_id), payload, evidence, digest((payload, evidence))),
    )


def capacity(db: sqlite3.Connection, plans: int, image_bytes: int) -> None:
    count, size = db.execute(
        "SELECT count(*),coalesce(sum(length(before_image)+length(after_image)),0) FROM plans"
    ).fetchone()
    if count + plans > MAX_COPY_PLANS or size + image_bytes > MAX_PLAN_BYTES:
        raise fail("limit_exceeded")


def insert(
    db: sqlite3.Connection,
    record: PatchRecord,
    prepared: PreparedPatch,
    owner_batch_id: UUID | None = None,
) -> None:
    db.execute(
        "INSERT INTO plans(id,request_id,proposal,before_image,after_image,owner_batch_id) "
        "VALUES(?,?,?,?,?,?)",
        (
            str(record.plan_id),
            record.request_id,
            prepared.proposal.model_dump_json(),
            prepared.before,
            prepared.after,
            str(owner_batch_id) if owner_batch_id is not None else None,
        ),
    )
    append(db, record, None)


def save(db: sqlite3.Connection, record: PatchRecord, prepared: PreparedPatch) -> None:
    with transaction(db):
        capacity(db, 1, len(prepared.before) + len(prepared.after))
        insert(db, record, prepared)


def load(
    db: sqlite3.Connection,
    workspace: Workspace,
    workspace_id: UUID,
    plan_id: UUID,
    operation: ReadOperation,
) -> tuple[PatchRecord, PreparedPatch, tuple[int, int] | None]:
    row = db.execute(
        "SELECT request_id,proposal,before_image,after_image FROM plans WHERE id=?", (str(plan_id),)
    ).fetchone()
    if row is None:
        raise fail("plan_not_found")
    try:
        events = db.execute(
            "SELECT payload,temporary,checksum FROM events WHERE plan_id=? "
            "ORDER BY sequence LIMIT 9",
            (str(plan_id),),
        ).fetchall()
        if not 1 <= len(events) <= 6:
            raise ValueError
        previous: PatchRecord | None = None
        last_identity: tuple[int, int] | None = None
        for payload, evidence, checksum in events:
            if checksum != digest((payload, evidence)):
                raise ValueError
            record = PatchRecord.model_validate_json(payload)
            temporary = None
            if evidence is not None:
                values = json.loads(evidence)
                if (
                    not isinstance(values, list)
                    or len(values) != 2
                    or any(type(v) is not int or v < 0 for v in values)
                ):
                    raise ValueError
                temporary = (values[0], values[1])
            if (
                record.plan_id != plan_id
                or record.workspace_id != workspace_id
                or record.request_id != row[0]
                or fingerprint(record) != record.approval_fingerprint
            ):
                raise ValueError
            if previous is None:
                if (
                    record.state != "pending"
                    or record.decision is not None
                    or temporary is not None
                ):
                    raise ValueError
            else:
                if record.model_dump(
                    exclude={"state", "decision", "error_code"}
                ) != previous.model_dump(exclude={"state", "decision", "error_code"}):
                    raise ValueError
                allowed = {
                    "pending": {"approved", "rejected"},
                    "approved": {"started"},
                    "started": {
                        "started",
                        "applied",
                        "failed",
                        "uncertain",
                        "observed_before",
                        "observed_after",
                        "diverged",
                        "missing",
                        "unavailable",
                    },
                    "uncertain": {
                        "observed_before",
                        "observed_after",
                        "diverged",
                        "missing",
                        "unavailable",
                    },
                }
                if record.state not in allowed.get(previous.state, set()):
                    raise ValueError
                if previous.decision is not None and record.decision != previous.decision:
                    raise ValueError
                if last_identity is not None and temporary != last_identity:
                    raise ValueError
                if temporary != last_identity and not (
                    previous.state == record.state == "started" and last_identity is None
                ):
                    raise ValueError
                if previous.state == record.state == "started" and (
                    temporary is None or last_identity is not None
                ):
                    raise ValueError
            if record.state != "pending":
                if record.decision is None or record.decision.outcome.value != (
                    "rejected" if record.state == "rejected" else "approved"
                ):
                    raise ValueError
            if record.state in {"applied", "observed_after"} and temporary is None:
                raise ValueError
            previous, last_identity = record, temporary
        assert previous is not None
        prepared = PreparedPatch(
            previous.manifest, PatchProposal.model_validate_json(row[1]), row[2], row[3]
        )
        validate_prepared(workspace, prepared, operation)
        return previous, prepared, last_identity
    except (ValueError, TypeError, AssertionError):
        raise fail("ledger_corrupt") from None
