"""真实Git账本的只读重开、写入隔离及原Reader校验边界。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import git_store as git_store_module
from harnessix.delivery.git_contracts import (
    GitCheckpoint,
    GitCommitRecord,
    ManagedGitWorktreeRecord,
)
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.sqlite_readonly import readonly_database
from tests.delivery.test_git import _COMMIT_TIME, _checkpoint, _close, _run

_TABLES = (
    "git_delivery_metadata",
    "git_worktrees",
    "git_worktree_events",
    "git_checkpoints",
    "git_commits",
    "git_commit_events",
)
_MUTATORS = (
    "save_worktree",
    "transition_worktree",
    "save_checkpoint",
    "save_commit",
    "transition_commit",
)
# 冻结ece88ad中的原v1 DDL，正控不依赖当前Writer或结构验证常量。
_ORIGINAL_V1_DDL = """
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
DeliverySnapshot = tuple[Path, ManagedGitWorktreeRecord, GitCheckpoint, GitCommitRecord]


def _commit(values: tuple[Any, ...]) -> GitCommitRecord:
    repository = values[0]
    runtime, lease, checkpoint = values[8], values[10], values[12]
    planned = runtime.plan_commit(
        checkpoint.checkpoint_id,
        repository,
        branch="harnessix/readonly-proof",
        author_name="Harnessix Agent",
        author_email="agent@harnessix.invalid",
        message="Read-only delivery proof",
        authored_at=_COMMIT_TIME,
    )
    committed = runtime.commit(
        planned.commit_id,
        repository,
        approval_fingerprint=planned.spec.fingerprint,
        lease=lease,
    )
    assert committed.state == "committed"
    assert _run(repository, "rev-parse", planned.spec.branch_ref).decode().strip() == (
        committed.commit_oid
    )
    assert _run(repository, "rev-parse", planned.spec.branch_ref + "^{tree}").decode().strip() == (
        checkpoint.tree_oid
    )
    assert (
        _run(repository, "rev-parse", planned.spec.branch_ref + "^").decode().strip() == (values[1])
    )
    assert _run(repository, "rev-parse", "HEAD").decode().strip() == values[1]
    assert _run(repository, "status", "--porcelain=v2", "-z") == b""
    return committed


def _delete_mode(root: Path) -> None:
    with closing(sqlite3.connect(root / "git-delivery.db")) as database:
        assert database.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)


@pytest.fixture(scope="module")
def git_delivery_snapshot(tmp_path_factory: pytest.TempPathFactory) -> DeliverySnapshot:
    values = _checkpoint(tmp_path_factory.mktemp("readonly-real-git"), request="readonly-proof")
    try:
        committed = _commit(values)
        root, worktree, checkpoint = values[3], values[11], values[12]
        assert values[6].load_worktree(worktree.worktree_id) == worktree
        assert values[6].load_checkpoint(checkpoint.checkpoint_id) == checkpoint
        assert values[6].load_commit(committed.commit_id) == committed
        assert values[6].save_worktree(worktree.plan) == worktree
        assert values[6].save_checkpoint(checkpoint) == checkpoint
        assert values[6].save_commit(committed.spec) == committed
    finally:
        _close(values)
    _delete_mode(root)
    return root, worktree, checkpoint, committed


@pytest.fixture
def store_copy(tmp_path: Path, git_delivery_snapshot: DeliverySnapshot) -> Path:
    return Path(shutil.copytree(git_delivery_snapshot[0], tmp_path / "git-state"))


def _snapshot(root: Path) -> dict[str, tuple[int, int | None, str | None]]:
    # 仅遍历本测试创建的临时目录；不扫描用户目录或其他工作者的账本。
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        info = path.stat()
        body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result[path.relative_to(root).as_posix()] = (
            stat.S_IMODE(info.st_mode),
            len(body) if body is not None else None,
            hashlib.sha256(body).hexdigest() if body is not None else None,
        )
    return result


def _business_rows(root: Path) -> dict[str, list[tuple[Any, ...]]]:
    with closing(readonly_database(root / "git-delivery.db")) as database:
        return {
            table: sorted(database.execute(f"SELECT * FROM {table}").fetchall())
            for table in _TABLES
        }


def _assert_readers(store: SQLiteGitDeliveryStore, snapshot: DeliverySnapshot) -> None:
    _, worktree, checkpoint, committed = snapshot
    for actual, expected in (
        (store.load_worktree(worktree.worktree_id), worktree),
        (store.load_checkpoint(checkpoint.checkpoint_id), checkpoint),
        (store.checkpoint_for_worktree(worktree.worktree_id), checkpoint),
        (store.load_commit(committed.commit_id), committed),
    ):
        assert actual is not None
        assert actual == expected
        assert actual.model_dump(mode="json", warnings="error") == expected.model_dump(
            mode="json", warnings="error"
        )


def test_official_original_v1_ddl_with_real_records_is_a_readonly_positive_control(
    tmp_path: Path, git_delivery_snapshot: DeliverySnapshot
) -> None:
    root = tmp_path / "original-v1"
    root.mkdir()
    rows = _business_rows(git_delivery_snapshot[0])
    with closing(sqlite3.connect(root / "git-delivery.db", isolation_level=None)) as database:
        database.executescript(_ORIGINAL_V1_DDL)
        for table, entries in rows.items():
            for row in entries:
                placeholders = ",".join("?" for _ in row)
                database.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)
        assert database.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    before = _snapshot(root)
    with SQLiteGitDeliveryStore(root, read_only=True) as reader:
        _assert_readers(reader, git_delivery_snapshot)
    assert _business_rows(root) == rows
    assert _snapshot(root) == before


def test_readonly_reopens_real_worktree_checkpoint_and_commit_without_physical_changes(
    store_copy: Path, git_delivery_snapshot: DeliverySnapshot
) -> None:
    nested = store_copy / "unrelated"
    nested.mkdir()
    marker = nested / "marker.bin"
    marker.write_bytes(b"unrelated\0artifact")
    if os.name == "posix":
        store_copy.chmod(0o755)
        (store_copy / "git-delivery.db").chmod(0o644)
        nested.chmod(0o750)
        marker.chmod(0o640)
    before, rows = _snapshot(store_copy), _business_rows(store_copy)
    assert not (store_copy / "git-delivery.db-wal").exists()
    assert not (store_copy / "git-delivery.db-shm").exists()
    for _ in range(2):
        with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
            assert reader.root == store_copy
            assert reader._db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            _assert_readers(reader, git_delivery_snapshot)
        assert _snapshot(store_copy) == before
        assert _business_rows(store_copy) == rows


def test_readonly_reuses_readonly_database_and_never_enters_writer_setup(
    store_copy: Path, git_delivery_snapshot: DeliverySnapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = []

    def tracked_readonly(path: Path) -> sqlite3.Connection:
        database = readonly_database(path)
        opened.append((path, database))
        return database

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("只读路径进入了Writer初始化或文件权限修改")

    monkeypatch.setattr(git_store_module, "readonly_database", tracked_readonly)
    monkeypatch.setattr(SQLiteGitDeliveryStore, "_prepare_directory", staticmethod(forbidden))
    monkeypatch.setattr(SQLiteGitDeliveryStore, "_initialize", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "chmod", forbidden)
    before = _snapshot(store_copy)
    with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
        assert opened == [(store_copy / "git-delivery.db", reader._db)]
        assert reader._db.execute("PRAGMA query_only").fetchone() == (1,)
        _assert_readers(reader, git_delivery_snapshot)
    assert _snapshot(store_copy) == before
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0][1].execute("SELECT 1")


@pytest.mark.parametrize("method", _MUTATORS)
@pytest.mark.parametrize("invalid_arguments", [False, True], ids=["existing", "invalid"])
def test_all_mutators_reject_before_idempotency_or_argument_validation(
    store_copy: Path,
    git_delivery_snapshot: DeliverySnapshot,
    method: str,
    invalid_arguments: bool,
) -> None:
    _, worktree, checkpoint, committed = git_delivery_snapshot
    arguments = {
        "save_worktree": (worktree.plan,),
        "transition_worktree": (worktree, worktree),
        "save_checkpoint": (checkpoint,),
        "save_commit": (committed.spec,),
        "transition_commit": (committed, committed),
    }[method]
    if invalid_arguments:
        arguments = tuple(None for _ in arguments)
    before = _snapshot(store_copy)
    raw = (store_copy / "git-delivery.db").read_bytes()
    rows = _business_rows(store_copy)
    with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
        with pytest.raises(KernelError) as rejected:
            getattr(reader, method)(*arguments)
        assert rejected.value.code == "git_delivery_store_read_only"
        _assert_readers(reader, git_delivery_snapshot)
    assert (store_copy / "git-delivery.db").read_bytes() == raw
    assert _business_rows(store_copy) == rows
    assert _snapshot(store_copy) == before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE git_delivery_metadata SET value='2' WHERE key='schema_version'",
        "CREATE TABLE forbidden_write (value TEXT)",
    ],
    ids=["business-update", "ddl"],
)
def test_mode_ro_blocks_sql_writes_even_when_query_only_is_disabled(
    store_copy: Path, statement: str
) -> None:
    before, rows = _snapshot(store_copy), _business_rows(store_copy)
    with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
        assert reader._db.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError) as query_only:
            reader._db.execute(statement)
        assert query_only.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_READONLY
        reader._db.execute("PRAGMA query_only = OFF")
        assert reader._db.execute("PRAGMA query_only").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError) as mode_ro:
            reader._db.execute(statement)
        assert mode_ro.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_READONLY
        with pytest.raises(KernelError) as guarded:
            reader.save_checkpoint(None)
        assert guarded.value.code == "git_delivery_store_read_only"
    assert _business_rows(store_copy) == rows
    assert _snapshot(store_copy) == before


@pytest.mark.parametrize("root_kind", ["missing-directory", "empty-directory", "regular-file"])
def test_missing_database_preserves_operational_error_without_creating_paths(
    tmp_path: Path, root_kind: str
) -> None:
    root = tmp_path / "absent-parent" / "git-state"
    if root_kind == "empty-directory":
        root.mkdir(parents=True)
    elif root_kind == "regular-file":
        root.parent.mkdir()
        root.write_bytes(b"not a directory")
    before = _snapshot(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        SQLiteGitDeliveryStore(root, read_only=True)
    assert _snapshot(tmp_path) == before
    assert not (root / "git-delivery.db").exists()


@pytest.mark.parametrize("name", ["state#fragment", "state%25", "中文 状态", "中文 #% 状态"])
def test_windows_legal_uri_characters_select_the_exact_database(
    tmp_path: Path, git_delivery_snapshot: DeliverySnapshot, name: str
) -> None:
    root = Path(shutil.copytree(git_delivery_snapshot[0], tmp_path / name))
    before = _snapshot(tmp_path)
    with SQLiteGitDeliveryStore(root, read_only=True) as reader:
        _assert_readers(reader, git_delivery_snapshot)
    assert _snapshot(tmp_path) == before


@pytest.mark.skipif(os.name != "posix", reason="问号不是Windows合法文件名")
def test_posix_question_mark_uri_selects_the_exact_database(
    tmp_path: Path, git_delivery_snapshot: DeliverySnapshot
) -> None:
    root = Path(shutil.copytree(git_delivery_snapshot[0], tmp_path / "状态 ? #%"))
    before = _snapshot(tmp_path)
    with SQLiteGitDeliveryStore(root, read_only=True) as reader:
        _assert_readers(reader, git_delivery_snapshot)
    assert _snapshot(tmp_path) == before


def test_live_reader_observes_real_git_commit_still_only_in_writer_wal(tmp_path: Path) -> None:
    values = _checkpoint(tmp_path / "live", request="readonly-live-wal")
    writer, root, worktree, checkpoint = values[6], values[3], values[11], values[12]
    try:
        assert writer._db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        writer._db.execute("PRAGMA wal_autocheckpoint = 0")
        assert writer._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        main_file = root / "git-delivery.db"
        before = main_file.read_bytes()
        with SQLiteGitDeliveryStore(root, read_only=True) as reader:
            assert reader.load_worktree(worktree.worktree_id) == worktree
            assert reader.load_checkpoint(checkpoint.checkpoint_id) == checkpoint
            committed = _commit(values)
            assert main_file.read_bytes() == before
            assert (root / "git-delivery.db-wal").stat().st_size > 32
            # 活跃WAL读取允许SQLite协调SHM；这里不要求WAL/SHM物理零触碰。
            snapshot = (root, worktree, checkpoint, committed)
            _assert_readers(reader, snapshot)
            with SQLiteGitDeliveryStore(root, read_only=True) as reopened:
                _assert_readers(reopened, snapshot)
            offline = tmp_path / "main-only"
            offline.mkdir()
            shutil.copy2(main_file, offline / "git-delivery.db")
            with SQLiteGitDeliveryStore(offline, read_only=True) as main_only:
                assert main_only.load_checkpoint(checkpoint.checkpoint_id) == checkpoint
                with pytest.raises(KernelError) as missing:
                    main_only.load_commit(committed.commit_id)
                assert missing.value.code == "git_commit_not_found"
            assert main_file.read_bytes() == before
    finally:
        _close(values)


def _damage_schema(root: Path, table: str, fault: str) -> None:
    path = root / "git-delivery.db"
    if fault == "binary":
        path.write_bytes(b"not a sqlite database\0\xff" * 128)
        return
    if fault == "empty-file":
        path.write_bytes(b"")
        return
    with closing(sqlite3.connect(path, isolation_level=None)) as database:
        if fault == "version-2":
            database.execute("UPDATE git_delivery_metadata SET value='2'")
        elif fault == "missing-version":
            database.execute("DELETE FROM git_delivery_metadata WHERE key='schema_version'")
        elif fault == "extra-view":
            database.execute("CREATE VIEW extra_view AS SELECT key FROM git_delivery_metadata")
        elif fault == "extra-trigger":
            database.execute(
                "CREATE TRIGGER extra_trigger AFTER UPDATE ON git_delivery_metadata "
                "BEGIN SELECT 1; END"
            )
        elif fault == "explicit-index":
            database.execute("CREATE INDEX extra_index ON git_worktrees(state)")
        else:
            ddl = database.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            columns = [row[1] for row in database.execute(f"PRAGMA table_info({table})")]
            database.execute(f"DROP TABLE {table}")
            if fault == "wrong-columns":
                database.execute(f"CREATE TABLE {table} (unexpected TEXT) STRICT")
            elif fault == "view":
                expressions = ", ".join(f"NULL AS {column}" for column in columns)
                if table == "git_delivery_metadata":
                    expressions = "'schema_version' AS key, '1' AS value"
                database.execute(f"CREATE VIEW {table} AS SELECT {expressions}")
            elif fault != "missing":
                if fault == "not-strict":
                    changed = ddl.removesuffix(" STRICT")
                elif fault == "weakened-not-null":
                    changed = ddl.replace(" NOT NULL", "", 1)
                elif fault == "missing-unique":
                    changed = ddl.replace(" UNIQUE", "", 1)
                elif fault == "missing-foreign-key":
                    changed = ddl.replace(
                        ",\n    FOREIGN KEY(commit_id) REFERENCES git_commits(commit_id)", ""
                    )
                else:
                    assert fault == "reordered-primary-key"
                    changed = ddl.replace(
                        "PRIMARY KEY(worktree_id, sequence)", "PRIMARY KEY(sequence, worktree_id)"
                    )
                assert changed != ddl
                database.execute(changed)
                if table == "git_delivery_metadata":
                    database.execute(
                        "INSERT INTO git_delivery_metadata VALUES ('schema_version','1')"
                    )


_SCHEMA_FAULTS = [
    pytest.param(table, fault, "git_delivery_store_corrupt", id=f"{table}-{fault}")
    for table in _TABLES
    for fault in ("missing", "wrong-columns", "view", "not-strict", "weakened-not-null")
] + [
    pytest.param("git_commits", "missing-unique", "git_delivery_store_corrupt", id="unique"),
    pytest.param(
        "git_commit_events", "missing-foreign-key", "git_delivery_store_corrupt", id="foreign-key"
    ),
    pytest.param(
        "git_worktree_events",
        "reordered-primary-key",
        "git_delivery_store_corrupt",
        id="primary-key-order",
    ),
    pytest.param("", "version-2", "git_delivery_store_version", id="version-2"),
    pytest.param("", "missing-version", "git_delivery_store_corrupt", id="missing-version"),
    pytest.param("", "empty-file", "git_delivery_store_corrupt", id="empty-file"),
    pytest.param("", "binary", "git_delivery_store_corrupt", id="binary"),
    pytest.param("", "extra-view", "git_delivery_store_corrupt", id="extra-view"),
    pytest.param("", "extra-trigger", "git_delivery_store_corrupt", id="extra-trigger"),
    pytest.param("", "explicit-index", "git_delivery_store_corrupt", id="explicit-index"),
]


@pytest.mark.parametrize("table,fault,code", _SCHEMA_FAULTS)
def test_schema_faults_are_rejected_without_repair_and_close_the_connection(
    store_copy: Path, table: str, fault: str, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _damage_schema(store_copy, table, fault)
    before = _snapshot(store_copy)
    original_connect = sqlite3.connect
    opened = []

    def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database = original_connect(*args, **kwargs)
        opened.append(database)
        return database

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    with pytest.raises(KernelError) as rejected:
        SQLiteGitDeliveryStore(store_copy, read_only=True)
    assert rejected.value.code == code
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    with closing(original_connect(store_copy / "git-delivery.db", timeout=0.1)) as independent:
        assert independent.execute("PRAGMA database_list").fetchone()[1] == "main"
        if fault != "binary":
            independent.execute("BEGIN EXCLUSIVE")
            independent.execute("SELECT name FROM sqlite_schema").fetchall()
            independent.rollback()
    assert _snapshot(store_copy) == before


@pytest.mark.parametrize("error_type", [sqlite3.DatabaseError, RuntimeError])
def test_simulated_schema_validation_failure_closes_before_independent_connection(
    store_copy: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    before = _snapshot(store_copy)
    original_connect = sqlite3.connect
    opened = []

    class FailingValidationConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            if sql.lstrip().upper().startswith("SELECT"):
                raise error_type("模拟结构验证失败")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        database = original_connect(*args, **kwargs, factory=FailingValidationConnection)
        opened.append(database)
        return database

    monkeypatch.setattr(sqlite3, "connect", failing_connect)
    expected = KernelError if issubclass(error_type, sqlite3.Error) else error_type
    with pytest.raises(expected) as rejected:
        SQLiteGitDeliveryStore(store_copy, read_only=True)
    if isinstance(rejected.value, KernelError):
        assert rejected.value.code == "git_delivery_store_corrupt"
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        sqlite3.Connection.execute(opened[0], "SELECT 1")
    with closing(original_connect(store_copy / "git-delivery.db", timeout=0.1)) as independent:
        independent.execute("BEGIN EXCLUSIVE")
        assert independent.execute("SELECT value FROM git_delivery_metadata").fetchone() == ("1",)
        independent.rollback()
    assert _snapshot(store_copy) == before


_READERS = {
    "load_worktree": ("git_worktrees", "worktree_id", "plan_fingerprint", "record_digest"),
    "load_checkpoint": ("git_checkpoints", "checkpoint_id", "digest", "digest"),
    "checkpoint_for_worktree": ("git_checkpoints", "checkpoint_id", "digest", "digest"),
    "load_commit": ("git_commits", "commit_id", "spec_fingerprint", "record_digest"),
}


@pytest.mark.parametrize("method", _READERS)
@pytest.mark.parametrize("fault", ["payload", "summary", "model-digest"])
def test_readonly_preserves_original_payload_summary_and_model_digest_validation(
    store_copy: Path, git_delivery_snapshot: DeliverySnapshot, method: str, fault: str
) -> None:
    table, _, summary, digest = _READERS[method]
    _, worktree, checkpoint, committed = git_delivery_snapshot
    identity = {
        "load_worktree": worktree.worktree_id,
        "load_checkpoint": checkpoint.checkpoint_id,
        "checkpoint_for_worktree": worktree.worktree_id,
        "load_commit": committed.commit_id,
    }[method]
    with closing(sqlite3.connect(store_copy / "git-delivery.db", isolation_level=None)) as database:
        if fault == "summary":
            database.execute(f"UPDATE {table} SET {summary}=?", ("f" * 64,))
        else:
            payload = json.loads(database.execute(f"SELECT payload FROM {table}").fetchone()[0])
            if fault == "model-digest":
                payload[digest] = "f" * 64
            damaged = json.dumps(payload) if fault == "model-digest" else "{}"
            database.execute(f"UPDATE {table} SET payload=?", (damaged,))
            if method in {"load_worktree", "load_commit"}:
                events = "git_worktree_events" if method == "load_worktree" else "git_commit_events"
                database.execute(
                    f"UPDATE {events} SET payload=? "
                    f"WHERE sequence=(SELECT MAX(sequence) FROM {events})",
                    (damaged,),
                )
    before = _snapshot(store_copy)
    with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
        with pytest.raises(KernelError) as rejected:
            getattr(reader, method)(identity)
        assert rejected.value.code == "git_delivery_store_corrupt"
    assert _snapshot(store_copy) == before


@pytest.mark.parametrize("kind", ["worktree", "commit"])
@pytest.mark.parametrize("fault", ["last-event", "event-count", "older-event"])
def test_readonly_preserves_last_event_and_count_checks_not_all_event_authentication(
    store_copy: Path, git_delivery_snapshot: DeliverySnapshot, kind: str, fault: str
) -> None:
    record = git_delivery_snapshot[1 if kind == "worktree" else 3]
    table = f"git_{kind}_events"
    identity = getattr(record, f"{kind}_id")
    with closing(sqlite3.connect(store_copy / "git-delivery.db", isolation_level=None)) as database:
        assert record.sequence > 0
        if fault == "event-count":
            database.execute(f"DELETE FROM {table} WHERE sequence=0")
        else:
            sequence = record.sequence if fault == "last-event" else 0
            database.execute(f"UPDATE {table} SET payload='{{}}' WHERE sequence=?", (sequence,))
    before = _snapshot(store_copy)
    with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
        load = getattr(reader, f"load_{kind}")
        if fault == "older-event":
            # 原Reader只验证最后事件与事件总数，不新增全事件认证或MAC。
            assert load(identity) == record
        else:
            with pytest.raises(KernelError) as rejected:
                load(identity)
            assert rejected.value.code == "git_delivery_store_corrupt"
    assert _snapshot(store_copy) == before


def test_readonly_preserves_original_missing_record_results(store_copy: Path) -> None:
    before = _snapshot(store_copy)
    with SQLiteGitDeliveryStore(store_copy, read_only=True) as reader:
        assert reader.checkpoint_for_worktree(uuid4()) is None
        for method, code in (
            (reader.load_worktree, "git_worktree_not_found"),
            (reader.load_checkpoint, "git_checkpoint_not_found"),
            (reader.load_commit, "git_commit_not_found"),
        ):
            with pytest.raises(KernelError) as missing:
                method(uuid4())
            assert missing.value.code == code
    assert _snapshot(store_copy) == before
