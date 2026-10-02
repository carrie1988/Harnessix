"""GitDB v2真实SQLite结构合同，不以布尔或认证替代DDL、约束及事务证据。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.git_store_schema import verify_git_store_schema
from harnessix.delivery.git_store_schema_v2 import (
    GIT_PRODUCT_LINK_PHASES,
    GIT_STORE_V2_DDL,
    GIT_STORE_V2_MIGRATION_CHECKSUM,
    _create_git_store_v2_tables,
    verify_git_store_v2_schema,
)
from harnessix.sqlite_readonly import readonly_database
from tests.delivery.test_git_store_readonly import _ORIGINAL_V1_DDL

_MAX = 2**63 - 1
_CAP = 64 * 1024 * 1024
_UIDS = tuple(str(uuid4()) for _ in range(20))
_SHA = "a" * 64
_LINK = "git_product_links"
_EVENT = "git_product_link_events"
_BRIDGE = "git_native_bridge_index"
_INV = "git_object_inventories"
_INV_EVENT = "git_object_inventory_events"
_PUB = "git_record_publications"
_ANCHOR = "git_prefix_anchor"
_NEW = (_LINK, _EVENT, _BRIDGE, _INV, _INV_EVENT, _PUB, _ANCHOR)
_OLD = (
    "git_delivery_metadata",
    "git_worktrees",
    "git_worktree_events",
    "git_checkpoints",
    "git_commits",
    "git_commit_events",
)
_PHASES = tuple(
    (
        "prepared approved materials_ready anchor_intent anchor_ready native_patch_intent "
        "native_patch_prepared native_bridge_closed delivery_worktree_intent "
        "delivery_worktree_ready checkpoint_intent checkpoint_closed commit_intent "
        "commit_closed result_closed unknown diverged failed"
    ).split()
)
_KINDS = ("object_inventory", "product_link", "worktree_event", "checkpoint", "commit_event")


def _row(columns: str, values: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(columns.split(), values, strict=True))


_ROWS = {
    _LINK: _row(
        "route_id delivery_id thread_id turn_id call_id action_kind core_sha256 "
        "route_fingerprint phase sequence payload",
        (*_UIDS[:5], "checkpoint", _SHA, _SHA, "prepared", 0, "{}"),
    ),
    _EVENT: _row("route_id sequence phase payload", (_UIDS[0], 0, "prepared", "{}")),
    _BRIDGE: _row(
        "bridge_id transaction_id route_id anchor_id worktree_id bridge_sha256",
        (_UIDS[5], _UIDS[6], _UIDS[0], _UIDS[7], _UIDS[8], _SHA),
    ),
    _INV: _row(
        "inventory_id delivery_id route_id phase sequence inventory_sha256 scope_sha256 payload",
        (_UIDS[9], _UIDS[1], _UIDS[0], "materials_ready", 0, _SHA, _SHA, "{}"),
    ),
    _INV_EVENT: _row("inventory_id sequence phase payload", (_UIDS[9], 0, "materials_ready", "{}")),
    _PUB: _row(
        "record_kind record_id publication_epoch sequence delivery_id thread_id "
        "turn_id call_id route_id previous_prefix body_sha256 body_bytes "
        "prefix_sha256 seal",
        ("product_link", _UIDS[0], _UIDS[10], 1, *_UIDS[1:5], _UIDS[0], _SHA, _SHA, 2, _SHA, b"s"),
    ),
    _ANCHOR: _row(
        "singleton revision genesis_epoch body_bytes body_sha256 payload seal",
        (1, 0, _UIDS[11], 2, _SHA, "{}", b"s"),
    ),
}


def _columns(suffix: str, *names: str) -> list[tuple[str, str]]:
    columns = [(t, c) for t, row in _ROWS.items() for c in row]
    return [(t, c) for t, c in columns if (suffix and c.endswith(suffix)) or c in names]


_UUID_COLUMNS = _columns("_id", "publication_epoch", "genesis_epoch")
_DIGEST_COLUMNS = _columns("sha256", "route_fingerprint", "previous_prefix")
_INTEGER_COLUMNS = _columns("", "sequence", "revision", "body_bytes", "singleton")


def _insert(db: sqlite3.Connection, table: str, **changes: Any) -> None:
    row = {**_ROWS[table], **changes}
    db.execute(
        f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
        tuple(row.values()),
    )


def _v1(db: sqlite3.Connection) -> None:
    db.executescript(_ORIGINAL_V1_DDL)
    db.execute("INSERT INTO git_delivery_metadata VALUES ('schema_version','1')")


def _promote(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    _create_git_store_v2_tables(db)
    db.execute("UPDATE git_delivery_metadata SET value='2' WHERE key='schema_version'")
    db.execute("COMMIT")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        _v1(db)
        _promote(db)
        verify_git_store_v2_schema(db)
        yield db


def _parents(db: sqlite3.Connection, table: str) -> None:
    if table not in (_LINK, _ANCHOR):
        _insert(db, _LINK)
    if table == _INV_EVENT:
        _insert(db, _INV)


def test_empty_database_verification_rejects_without_creating_tables() -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        with pytest.raises(KernelError) as caught:
            verify_git_store_v2_schema(db)
        assert caught.value.code == "git_delivery_store_corrupt"
        assert db.execute("SELECT name FROM sqlite_schema").fetchall() == []
        assert not db.in_transaction
        assert not any(
            s.lstrip().upper().startswith(("CREATE", "INSERT", "BEGIN", "COMMIT"))
            for s in statements
        )


def test_helper_requires_existing_transaction_and_does_not_change_v1() -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        _v1(db)
        before = db.serialize()
        with pytest.raises(KernelError) as caught:
            _create_git_store_v2_tables(db)
        assert caught.value.code == "git_delivery_store_transaction_required"
        assert db.serialize() == before
        verify_git_store_schema(db)


def test_idempotent_same_transaction_and_legacy_rollback() -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        _v1(db)
        payload = ' { "legacy" : "\\u4e2d" }\n'
        legacy_rows = (
            (_OLD[1], (_UIDS[0], _UIDS[1], _SHA, "prepared", 0, payload)),
            (_OLD[2], (_UIDS[0], 0, "prepared", payload)),
            (_OLD[3], (_UIDS[5], _UIDS[0], _UIDS[1], _SHA, payload)),
            (_OLD[4], (_UIDS[6], _UIDS[5], "refs/heads/x", _SHA, "prepared", 0, payload)),
            (_OLD[5], (_UIDS[6], 0, "prepared", payload)),
        )
        for table, values in legacy_rows:
            db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values)
        db.execute("INSERT INTO git_delivery_metadata VALUES ('opaque_legacy','unchanged')")
        rows = {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in _OLD}
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE git_delivery_metadata SET value='pending' WHERE key='opaque_legacy'")
        trace: list[str] = []
        db.set_trace_callback(trace.append)
        _create_git_store_v2_tables(db)
        _create_git_store_v2_tables(db)
        assert db.in_transaction
        assert db.execute(
            "SELECT value FROM git_delivery_metadata WHERE key='schema_version'"
        ).fetchone() == ("1",)
        assert db.execute("SELECT payload FROM git_worktrees").fetchone() == (payload,)
        assert sum(s.startswith("CREATE TABLE") for s in trace) == 7
        assert not any(
            s.startswith(("BEGIN", "COMMIT", "ROLLBACK", "INSERT", "UPDATE")) for s in trace
        )
        db.execute("ROLLBACK")
        assert {t: db.execute(f"SELECT * FROM {t}").fetchall() for t in _OLD} == rows
        verify_git_store_schema(db)


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("forced"), KeyboardInterrupt()])
def test_mid_ddl_failure_and_caller_rollback(failure: BaseException) -> None:
    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("CREATE TABLE git_native_bridge_index"):
                raise failure
            return super().execute(sql, parameters)

    with closing(
        sqlite3.connect(":memory:", isolation_level=None, factory=InterruptedConnection)
    ) as db:
        _v1(db)
        db.execute("BEGIN")
        try:
            _create_git_store_v2_tables(db)
        except KernelError as error:
            assert isinstance(failure, sqlite3.Error) and error.code == "git_delivery_store_corrupt"
        except KeyboardInterrupt as error:
            assert error is failure
        else:
            pytest.fail("未触发真实DDL失败")
        assert db.in_transaction
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='git_product_links'").fetchone()
        db.execute("ROLLBACK")
        verify_git_store_schema(db)


def test_exact_13_tables_columns_and_autoindexes(db: sqlite3.Connection) -> None:
    tables = {row[1]: row for row in db.execute("PRAGMA table_list") if row[1] in (*_OLD, *_NEW)}
    assert set(tables) == set((*_OLD, *_NEW)) and all(row[5] == 1 for row in tables.values())
    for t, values in _ROWS.items():
        columns = db.execute(f"PRAGMA table_xinfo({t})").fetchall()
        assert [c[1] for c in columns] == list(values)
        assert all(c[3] == 1 and c[6] == 0 for c in columns)
        scalar_types = {
            "seal": "BLOB",
            "singleton": "INT",
            "sequence": "INTEGER",
            "revision": "INTEGER",
            "body_bytes": "INTEGER",
        }
        assert [c[2] for c in columns] == [scalar_types.get(n, "TEXT") for n in values]
    sql = "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
    assert db.execute(sql).fetchall() == []
    checksum = hashlib.sha256(GIT_STORE_V2_DDL.encode("ascii")).hexdigest()
    assert GIT_STORE_V2_MIGRATION_CHECKSUM == checksum
    assert len(db.execute("SELECT name FROM sqlite_schema WHERE type='index'").fetchall()) == 23
    assert GIT_STORE_V2_DDL.endswith(";\n") and GIT_STORE_V2_DDL.count(";\n") == 13
    assert "IF NOT EXISTS" not in GIT_STORE_V2_DDL and "\r" not in GIT_STORE_V2_DDL


@pytest.mark.parametrize(("table", "column"), [(t, c) for t, row in _ROWS.items() for c in row])
def test_every_new_column_is_required(db: sqlite3.Connection, table: str, column: str) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **{column: None})


@pytest.mark.parametrize(("table", "column"), _UUID_COLUMNS)
@pytest.mark.parametrize("value", ["", "x" * 35, "x" * 37, b"x" * 36])
def test_uuid_length_and_text_type(
    db: sqlite3.Connection, table: str, column: str, value: object
) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **{column: value})


@pytest.mark.parametrize(("table", "column"), _DIGEST_COLUMNS)
@pytest.mark.parametrize("value", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, b"a" * 64])
def test_all_sha_columns_are_lowerhex64_text(
    db: sqlite3.Connection, table: str, column: str, value: object
) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **{column: value})


@pytest.mark.parametrize(("table", "column"), _INTEGER_COLUMNS)
@pytest.mark.parametrize("value", [-1, "invalid", 1.5, str(_MAX + 1)])
def test_integer_ranges_and_strict_types(
    db: sqlite3.Connection, table: str, column: str, value: object
) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **{column: value})


@pytest.mark.parametrize("phase", _PHASES)
@pytest.mark.parametrize("table", [_LINK, _EVENT])
def test_exact_link_phase_union_accepts_declarations_not_transitions(
    db: sqlite3.Connection, table: str, phase: str
) -> None:
    assert GIT_PRODUCT_LINK_PHASES == _PHASES
    _parents(db, table)
    _insert(db, table, phase=phase, sequence=_MAX)


@pytest.mark.parametrize(
    ("table", "changes"),
    [
        (_LINK, {"phase": "future"}),
        (_EVENT, {"phase": "future"}),
        (_LINK, {"action_kind": "push"}),
        (_INV, {"phase": "ready"}),
        (_INV_EVENT, {"phase": "closed"}),
        (_PUB, {"record_kind": "future"}),
        (_PUB, {"sequence": 0}),
        (_PUB, {"record_id": _UIDS[19]}),
        (_ANCHOR, {"singleton": 2}),
        (_ANCHOR, {"body_bytes": 1}),
        (_ANCHOR, {"body_bytes": 0}),
        (_PUB, {"body_bytes": 0}),
        (_PUB, {"body_bytes": _CAP + 1}),
    ],
)
def test_finite_enums_singleton_and_body_byte_relationships(
    db: sqlite3.Connection, table: str, changes: dict[str, Any]
) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **changes)


@pytest.mark.parametrize("kind", _KINDS)
def test_publication_polymorphism_is_not_a_fake_entity_fk(
    db: sqlite3.Connection, kind: str
) -> None:
    _parents(db, _PUB)
    _insert(
        db,
        _PUB,
        record_kind=kind,
        record_id=_UIDS[0] if kind == "product_link" else _UIDS[18],
        sequence=_MAX,
        body_bytes=_CAP,
        seal=b"s",
    )
    assert db.execute("SELECT body_bytes,length(seal) FROM git_record_publications").fetchone() == (
        _CAP,
        1,
    )


@pytest.mark.parametrize(
    ("table", "changes"),
    [
        (_EVENT, {"route_id": _UIDS[19]}),
        (_BRIDGE, {"route_id": _UIDS[19]}),
        (_INV, {"delivery_id": _UIDS[19]}),
        (_INV, {"route_id": _UIDS[19]}),
        (_INV_EVENT, {"inventory_id": _UIDS[19]}),
        *[
            (_PUB, {c: _UIDS[19]})
            for c in ("delivery_id", "thread_id", "turn_id", "call_id", "route_id")
        ],
    ],
)
def test_real_foreign_keys_reject_missing_or_mixed_redundant_identity(
    db: sqlite3.Connection, table: str, changes: dict[str, Any]
) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(
            db,
            table,
            **{**changes, **({"record_kind": "checkpoint"} if table == _PUB else {})},
        )


@pytest.mark.parametrize(
    ("table", "changes"),
    [
        (_LINK, {"delivery_id": _UIDS[19], "call_id": _UIDS[18]}),
        (_LINK, {"route_id": _UIDS[19], "call_id": _UIDS[18]}),
        (_LINK, {"route_id": _UIDS[19], "delivery_id": _UIDS[18]}),
        (_EVENT, {}),
        (_BRIDGE, {}),
        (_BRIDGE, {"bridge_id": _UIDS[19]}),
        (_INV, {}),
        (_INV_EVENT, {}),
        (_PUB, {}),
        (_ANCHOR, {}),
    ],
)
def test_primary_keys_and_unique_constraints_enforce_actual_inserts(
    db: sqlite3.Connection, table: str, changes: dict[str, Any]
) -> None:
    _parents(db, table)
    _insert(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **changes)


def test_bridge_future_worktree_and_two_inventories_are_not_overconstrained(
    db: sqlite3.Connection,
) -> None:
    _insert(db, _LINK)
    _insert(db, _BRIDGE)
    assert db.execute("SELECT count(*) FROM git_worktrees").fetchone() == (0,)
    _insert(db, _INV)
    _insert(db, _INV, inventory_id=_UIDS[19], phase="effect_closed", sequence=1)
    assert db.execute("SELECT count(*) FROM git_object_inventories").fetchone() == (2,)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("table", [_PUB, _ANCHOR])
@pytest.mark.parametrize("seal", [b"", b"x" * 4097, "x", 3])
def test_seal_is_nonempty_blob_with_maximum4096(
    db: sqlite3.Connection, table: str, seal: object
) -> None:
    _parents(db, table)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, seal=seal)


@pytest.mark.parametrize("table", [_PUB, _ANCHOR])
def test_seal_exact4096_is_accepted(db: sqlite3.Connection, table: str) -> None:
    _parents(db, table)
    _insert(db, table, seal=b"x" * 4096)


@pytest.mark.parametrize("table", [_LINK, _EVENT, _INV, _INV_EVENT, _ANCHOR])
def test_multibyte_payload_over_64mib_is_rejected_by_bytes_not_characters(
    db: sqlite3.Connection, table: str
) -> None:
    _parents(db, table)
    payload = "汉" * (_CAP // 3 + 1)
    assert len(payload) < _CAP < len(payload.encode("utf-8"))
    changes: dict[str, Any] = {"payload": payload}
    if table == _ANCHOR:
        changes["body_bytes"] = _CAP
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, table, **changes)


def test_multibyte_anchor_exact_64mib_and_body_byte_match(db: sqlite3.Connection) -> None:
    payload = "汉" * (_CAP // 3) + "x" * (_CAP % 3)
    assert len(payload.encode()) == _CAP
    _insert(db, _ANCHOR, payload=payload, body_bytes=_CAP)
    assert db.execute(
        "SELECT body_bytes,length(CAST(payload AS BLOB)) FROM git_prefix_anchor"
    ).fetchone() == (_CAP, _CAP)


@pytest.mark.parametrize(
    "extra",
    [
        "CREATE VIEW extra AS SELECT 1",
        "CREATE INDEX extra ON git_product_links(phase)",
        "CREATE TRIGGER extra AFTER INSERT ON git_product_links BEGIN SELECT 1; END",
        "CREATE TABLE extra(x TEXT)",
        "CREATE TEMP VIEW extra AS SELECT 1",
    ],
)
def test_extra_structure_is_rejected_without_repair(db: sqlite3.Connection, extra: str) -> None:
    db.execute(extra)
    db.execute("BEGIN")
    for operation in (verify_git_store_v2_schema, _create_git_store_v2_tables):
        with pytest.raises(KernelError) as caught:
            operation(db)
        assert caught.value.code == "git_delivery_store_corrupt"
    assert db.in_transaction
    db.execute("ROLLBACK")


@pytest.mark.parametrize("table", _OLD + _NEW)
def test_missing_any_of_13_tables_is_not_repaired(db: sqlite3.Connection, table: str) -> None:
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(f"DROP TABLE {table}")
    db.execute("BEGIN")
    with pytest.raises(KernelError):
        _create_git_store_v2_tables(db)
    with pytest.raises(KernelError):
        verify_git_store_v2_schema(db)
    assert db.execute("SELECT name FROM sqlite_schema WHERE name=?", (table,)).fetchone() is None
    db.execute("ROLLBACK")


@pytest.mark.parametrize("table", _NEW)
def test_constraint_ddl_drift_is_rejected(db: sqlite3.Connection, table: str) -> None:
    sql = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
    db.execute("PRAGMA writable_schema=ON")
    db.execute("UPDATE sqlite_schema SET sql=? WHERE name=?", (sql.removesuffix(" STRICT"), table))
    db.execute("PRAGMA writable_schema=OFF")
    with pytest.raises(KernelError) as caught:
        verify_git_store_v2_schema(db)
    assert caught.value.code == "git_delivery_store_corrupt"


@pytest.mark.parametrize("version", ["1", "3", "unknown", "02"])
def test_v2_verifier_rejects_other_versions_without_touching_rows(
    db: sqlite3.Connection, version: str
) -> None:
    db.execute("UPDATE git_delivery_metadata SET value=?", (version,))
    before = db.serialize()
    with pytest.raises(KernelError) as caught:
        verify_git_store_v2_schema(db)
    assert caught.value.code == "git_delivery_store_version"
    assert db.serialize() == before


def test_legacy_store_still_defaults_v1_and_refuses_v2(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    with SQLiteGitDeliveryStore(root):
        pass
    with closing(sqlite3.connect(root / "git-delivery.db", isolation_level=None)) as db:
        verify_git_store_schema(db)
        _promote(db)
        verify_git_store_v2_schema(db)
    for readonly in (False, True):
        with pytest.raises(KernelError) as caught:
            SQLiteGitDeliveryStore(root, read_only=readonly)
        assert caught.value.code == "git_delivery_store_version"


def test_readonly_file_and_no_business_reads(tmp_path: Path) -> None:
    root = tmp_path / "readonly"
    root.mkdir(mode=0o700)
    path = root / "git-delivery.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        _v1(db)
        _promote(db)
    path.chmod(0o600)
    before = (path.read_bytes(), sorted(root.iterdir()))
    with closing(readonly_database(path)) as db:
        trace: list[str] = []
        db.set_trace_callback(trace.append)

        def authorize(
            action: int, first: str | None, second: str | None, name: str | None, source: str | None
        ) -> int:
            if action == sqlite3.SQLITE_READ and first in (*_OLD[1:], *_NEW):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        assert verify_git_store_v2_schema(db) is None
        assert not db.in_transaction
        assert all(s.startswith("SELECT") or s == "PRAGMA main.encoding" for s in trace)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("CREATE TABLE prohibited(x TEXT)")
    assert (path.read_bytes(), sorted(root.iterdir())) == before


@pytest.mark.parametrize("table", [_INV, _PUB])
def test_composite_fk_rejects_mix_of_two_existing_links(db: sqlite3.Connection, table: str) -> None:
    _insert(db, _LINK)
    _insert(
        db,
        _LINK,
        route_id=_UIDS[19],
        delivery_id=_UIDS[18],
        thread_id=_UIDS[17],
        turn_id=_UIDS[16],
        call_id=_UIDS[15],
    )
    changes = {"delivery_id": _UIDS[18]} if table == _INV else {"thread_id": _UIDS[17]}
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(db, table, **changes)


@pytest.mark.parametrize("operation", [verify_git_store_v2_schema, _create_git_store_v2_tables])
def test_metadata_view_is_rejected_before_any_observation(operation: Any) -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        _v1(db)
        calls: list[int] = []
        db.create_function("parent_touch_metadata", 0, lambda: calls.append(1) or "2")
        db.execute("ALTER TABLE git_delivery_metadata RENAME TO hidden_metadata")
        db.execute(
            "CREATE VIEW git_delivery_metadata AS SELECT 'schema_version' AS key, "
            "parent_touch_metadata() AS value"
        )
        db.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            operation(db)
        assert caught.value.code == "git_delivery_store_corrupt"
        assert calls == []
        db.execute("ROLLBACK")


@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
@pytest.mark.parametrize("operation", [verify_git_store_v2_schema, _create_git_store_v2_tables])
def test_utf16_rejected_without_changes(encoding: str, operation: Any) -> None:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute(f"PRAGMA encoding='{encoding}'")
        _v1(db)
        if operation is verify_git_store_v2_schema:
            db.executescript(GIT_STORE_V2_DDL.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
            db.execute("UPDATE git_delivery_metadata SET value='2'")
        assert db.execute("PRAGMA main.encoding").fetchone() == (encoding,)
        payload = "汉" * (_CAP // 3 + 1)
        assert len(payload.encode("utf-8")) > _CAP
        assert db.execute("SELECT length(CAST(? AS BLOB))", (payload,)).fetchone()[0] < _CAP
        db.execute("BEGIN")
        before = (db.serialize(), db.total_changes)
        with pytest.raises(KernelError) as caught:
            operation(db)
        assert caught.value.code == "git_delivery_store_corrupt"
        assert db.in_transaction and (db.serialize(), db.total_changes) == before
        db.execute("ROLLBACK")
