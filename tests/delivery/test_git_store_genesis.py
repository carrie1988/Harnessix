"""验证空Git账本结构初始化、拒绝边界与调用者事务归属。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import git_store_genesis
from harnessix.delivery.git_store_genesis import initialize_git_store_v2
from harnessix.delivery.git_store_schema import initialize_git_store, verify_git_store_schema
from harnessix.delivery.git_store_schema_v2 import (
    GIT_STORE_V2_DDL,
    _create_git_store_v2_tables,
    verify_git_store_v2_schema,
)


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        yield connection
    finally:
        connection.close()


def _prepare(database: sqlite3.Connection, kind: str) -> None:
    if kind == "fresh":
        return
    initialize_git_store(database)
    if kind == "v2":
        database.execute("BEGIN")
        _create_git_store_v2_tables(database)
        database.execute("UPDATE git_delivery_metadata SET value='2' WHERE key='schema_version'")
        database.commit()


def _snapshot(database: sqlite3.Connection) -> tuple[object, ...]:
    schema = tuple(
        database.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema ORDER BY name"
        ).fetchall()
    )
    rows = tuple(
        (name, tuple(database.execute(f'SELECT * FROM main."{name}"').fetchall()))
        for kind, name, _, _ in schema
        if kind == "table"
    )
    temporary = tuple(
        database.execute(
            "SELECT type,name,tbl_name,sql FROM temp.sqlite_schema ORDER BY name"
        ).fetchall()
    )
    return schema, rows, temporary


def _assert_error_without_changes(
    database: sqlite3.Connection, code: str = "git_delivery_store_corrupt"
) -> None:
    before = _snapshot(database)
    changes = database.total_changes
    trace: list[str] = []
    database.set_trace_callback(trace.append)
    try:
        with pytest.raises(KernelError) as caught:
            initialize_git_store_v2(database)
    finally:
        database.set_trace_callback(None)
    assert caught.value.code == code
    assert database.in_transaction
    assert database.total_changes == changes
    assert _snapshot(database) == before
    assert not any(
        sql.split()[0].upper() in {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
        for sql in trace
    )


def _assert_empty_v2(database: sqlite3.Connection) -> None:
    verify_git_store_v2_schema(database)
    assert database.execute("SELECT * FROM git_delivery_metadata").fetchall() == [
        ("schema_version", "2")
    ]
    tables = database.execute("SELECT name FROM main.sqlite_schema WHERE type='table'").fetchall()
    assert len(tables) == 13
    for (name,) in tables:
        if name != "git_delivery_metadata":
            assert database.execute(f"SELECT 1 FROM main.{name} LIMIT 1").fetchone() is None


@pytest.mark.parametrize("begin", ["BEGIN", "BEGIN IMMEDIATE", "SAVEPOINT caller"])
def test_fresh_database_initializes_inside_caller_transaction(
    database: sqlite3.Connection, begin: str
) -> None:
    database.execute(begin)
    assert initialize_git_store_v2(database) is None
    assert database.in_transaction
    _assert_empty_v2(database)
    database.rollback()
    assert database.execute("SELECT 1 FROM main.sqlite_schema").fetchone() is None


def test_empty_v1_initializes_without_changing_original_ddl(database: sqlite3.Connection) -> None:
    _prepare(database, "v1")
    original = database.execute(
        "SELECT name,sql FROM main.sqlite_schema WHERE type='table' ORDER BY name"
    ).fetchall()
    database.execute("BEGIN")
    initialize_git_store_v2(database)
    assert database.in_transaction
    _assert_empty_v2(database)
    for name, sql in original:
        assert database.execute(
            "SELECT sql FROM main.sqlite_schema WHERE name=?", (name,)
        ).fetchone() == (sql,)
    database.commit()
    verify_git_store_v2_schema(database)


@pytest.mark.parametrize("kind", ["fresh", "v1", "v2"])
def test_transaction_is_required_even_for_complete_v2(
    database: sqlite3.Connection, kind: str
) -> None:
    _prepare(database, kind)
    before = _snapshot(database)
    with pytest.raises(KernelError) as caught:
        initialize_git_store_v2(database)
    assert caught.value.code == "git_delivery_store_transaction_required"
    assert not database.in_transaction
    assert _snapshot(database) == before


@pytest.mark.parametrize(
    "insert",
    [
        "INSERT INTO git_worktrees VALUES ('w','t','p','prepared',0,'{}')",
        "INSERT INTO git_worktree_events VALUES ('w',0,'prepared','{}')",
        "INSERT INTO git_checkpoints VALUES ('c','w','t','d','{}')",
        "INSERT INTO git_commits VALUES ('m','c','refs/heads/test','s','prepared',0,'{}')",
        "INSERT INTO git_commit_events VALUES ('m',0,'prepared','{}')",
    ],
)
def test_any_v1_business_record_is_legacy_unproven(
    database: sqlite3.Connection, insert: str
) -> None:
    _prepare(database, "v1")
    # 孤立事件也必须拒绝，不依赖外键开关或有效业务载荷。
    database.execute(insert)
    database.execute("BEGIN")
    database.execute("PRAGMA user_version=17")
    _assert_error_without_changes(database, "git_delivery_store_legacy_unproven")
    assert database.execute("PRAGMA user_version").fetchone() == (17,)
    verify_git_store_schema(database)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE unrelated (value TEXT)",
        "CREATE VIEW unrelated AS SELECT 1",
        "CREATE TEMP TABLE git_delivery_metadata (key TEXT, value TEXT)",
        "PRAGMA encoding='UTF-16le'",
    ],
)
def test_unknown_fresh_schema_is_rejected_without_repair(
    database: sqlite3.Connection, sql: str
) -> None:
    database.execute(sql)
    database.execute("BEGIN")
    _assert_error_without_changes(database)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DROP TABLE git_commit_events", "git_delivery_store_corrupt"),
        ("ALTER TABLE git_worktrees ADD COLUMN extra TEXT", "git_delivery_store_corrupt"),
        ("CREATE TABLE unrelated (value TEXT)", "git_delivery_store_corrupt"),
        ("CREATE INDEX extra ON git_worktrees(state)", "git_delivery_store_corrupt"),
        ("CREATE VIEW extra AS SELECT 1", "git_delivery_store_corrupt"),
        (
            "CREATE TRIGGER extra AFTER INSERT ON git_worktrees BEGIN SELECT 1; END",
            "git_delivery_store_corrupt",
        ),
        ("CREATE TEMP TABLE extra (value TEXT)", "git_delivery_store_corrupt"),
        ("DELETE FROM git_delivery_metadata", "git_delivery_store_corrupt"),
        ("UPDATE git_delivery_metadata SET value='2'", "git_delivery_store_corrupt"),
        ("UPDATE git_delivery_metadata SET value='3'", "git_delivery_store_version"),
        (
            "INSERT INTO git_delivery_metadata VALUES ('extra','value')",
            "git_delivery_store_corrupt",
        ),
        (GIT_STORE_V2_DDL.split(";")[6], "git_delivery_store_corrupt"),
    ],
)
def test_partial_unknown_or_cross_version_v1_is_rejected_without_repair(
    database: sqlite3.Connection, sql: str, code: str
) -> None:
    _prepare(database, "v1")
    database.execute(sql)
    database.execute("BEGIN")
    _assert_error_without_changes(database, code)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE git_record_publications",
        "ALTER TABLE git_prefix_anchor ADD COLUMN extra TEXT",
        "UPDATE git_delivery_metadata SET value='1'",
        "CREATE TABLE unrelated (value TEXT)",
    ],
)
def test_partial_unknown_or_cross_version_v2_is_rejected_without_repair(
    database: sqlite3.Connection, sql: str
) -> None:
    _prepare(database, "v2")
    database.execute(sql)
    database.execute("BEGIN")
    _assert_error_without_changes(database)


def test_malformed_metadata_is_rejected_before_view_execution(database: sqlite3.Connection) -> None:
    _prepare(database, "v1")
    database.execute("DROP TABLE git_delivery_metadata")
    executed: list[str] = []
    database.create_function("unexpected", 0, lambda: executed.append("执行") or "1")
    database.execute(
        "CREATE VIEW git_delivery_metadata AS SELECT 'schema_version' AS key, unexpected() AS value"
    )
    database.execute("BEGIN")
    _assert_error_without_changes(database)
    assert executed == []


def test_duplicate_version_rows_with_malformed_metadata_are_rejected(
    database: sqlite3.Connection,
) -> None:
    _prepare(database, "v1")
    database.execute("DROP TABLE git_delivery_metadata")
    database.execute("CREATE TABLE git_delivery_metadata (key TEXT, value TEXT) STRICT")
    database.executemany(
        "INSERT INTO git_delivery_metadata VALUES (?,?)",
        [("schema_version", "1"), ("schema_version", "1")],
    )
    database.execute("BEGIN")
    _assert_error_without_changes(database)


@pytest.mark.parametrize("kind", ["fresh", "v1"])
def test_caller_rollback_undoes_initialization_and_prior_pending_changes(
    database: sqlite3.Connection, kind: str
) -> None:
    _prepare(database, kind)
    before = _snapshot(database)
    database.execute("BEGIN")
    database.execute("PRAGMA user_version=17")
    initialize_git_store_v2(database)
    assert database.in_transaction
    _assert_empty_v2(database)
    database.rollback()
    assert _snapshot(database) == before
    assert database.execute("PRAGMA user_version").fetchone() == (0,)


def test_caller_savepoint_is_preserved(database: sqlite3.Connection) -> None:
    _prepare(database, "v1")
    before = _snapshot(database)
    database.execute("BEGIN")
    database.execute("PRAGMA user_version=17")
    database.execute("SAVEPOINT caller")
    initialize_git_store_v2(database)
    database.execute("ROLLBACK TO caller")
    assert _snapshot(database) == before
    assert database.in_transaction
    assert database.execute("PRAGMA user_version").fetchone() == (17,)
    database.execute("RELEASE caller")
    database.commit()


@pytest.mark.parametrize("has_anchor", [False, True])
def test_complete_v2_is_reverified_without_authentication_initialization(
    database: sqlite3.Connection, has_anchor: bool
) -> None:
    _prepare(database, "v2")
    # 认证记录及业务有效性属于其他边界，重复核验不得读取、修补或替换它们。
    database.execute("INSERT INTO git_worktrees VALUES ('w','t','p','prepared',0,'非认证旧载荷')")
    database.execute("INSERT INTO git_delivery_metadata VALUES ('authentication','未认证')")
    if has_anchor:
        # 固定夹具只满足结构约束；本测试不生成或验证MAC。
        database.execute(
            "INSERT INTO git_prefix_anchor VALUES (1,0,?,?,?,?,?)",
            ("00000000-0000-0000-0000-000000000001", 2, "0" * 64, "{}", b"caller-MAC"),
        )
    database.execute("BEGIN")
    before = _snapshot(database)
    changes = database.total_changes
    assert initialize_git_store_v2(database) is None
    assert initialize_git_store_v2(database) is None
    assert database.in_transaction
    assert database.total_changes == changes
    assert _snapshot(database) == before
    assert database.execute("SELECT count(*) FROM git_prefix_anchor").fetchone() == (
        int(has_anchor),
    )
    assert database.execute("SELECT * FROM git_record_publications").fetchall() == []


@pytest.mark.parametrize("kind", ["fresh", "v1", "v2"])
def test_query_only_connection_is_rejected_without_changes(
    database: sqlite3.Connection, kind: str
) -> None:
    _prepare(database, kind)
    database.execute("PRAGMA query_only=ON")
    database.execute("BEGIN")
    _assert_error_without_changes(database, "git_delivery_store_read_only")


@pytest.mark.parametrize("kind", ["fresh", "v1", "v2"])
def test_readonly_uri_is_rejected_without_database_or_sidecar_changes(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "git-delivery.db"
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        _prepare(writer, kind)
    finally:
        writer.close()
    before_bytes = path.read_bytes()
    before_paths = sorted(p.name for p in tmp_path.iterdir())
    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        assert reader.execute("PRAGMA query_only").fetchone() == (0,)
        reader.execute("BEGIN")
        _assert_error_without_changes(reader, "git_delivery_store_read_only")
        assert path.read_bytes() == before_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == before_paths
    finally:
        reader.close()


def test_creation_failure_does_not_commit_or_rollback_caller_transaction(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(database, "v1")
    before = _snapshot(database)
    database.execute("BEGIN")
    database.execute("PRAGMA user_version=17")

    def fail_after_creation(connection: sqlite3.Connection) -> None:
        _create_git_store_v2_tables(connection)
        raise sqlite3.OperationalError("模拟建表后的失败")

    monkeypatch.setattr(git_store_genesis, "_create_git_store_v2_tables", fail_after_creation)
    with pytest.raises(KernelError) as caught:
        initialize_git_store_v2(database)
    assert caught.value.code == "git_delivery_store_corrupt"
    assert database.in_transaction
    assert database.execute("PRAGMA user_version").fetchone() == (17,)
    assert database.execute("SELECT * FROM git_delivery_metadata").fetchall() == [
        ("schema_version", "1")
    ]
    database.rollback()
    assert _snapshot(database) == before
    assert database.execute("PRAGMA user_version").fetchone() == (0,)
