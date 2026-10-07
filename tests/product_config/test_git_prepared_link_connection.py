"""专用原 SQLite 连接的来源、物理置换、生命周期及调用方事务回归。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_link_connection as connection
from harnessix.product_config.git_prepared_link_connection import (
    open_prepared_git_connection,
    require_prepared_git_connection,
)


def _database(path: Path, value: str = "A") -> Path:
    with closing(sqlite3.connect(path)) as database:
        database.execute("CREATE TABLE records (value TEXT NOT NULL)")
        database.execute("INSERT INTO records VALUES (?)", (value,))
        database.commit()
    return path


@contextmanager
def _invalid():
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == "git_prepared_link_host_invalid"


def _closed(database):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("name", ["git-delivery.db", "Git 中文 ?#%.db"])
def test_existing_database_uses_original_connection_without_creating_files(
    tmp_path, read_only, name
):
    path = _database(tmp_path / name)
    before = path.read_bytes(), set(tmp_path.iterdir())
    with open_prepared_git_connection(path, read_only=read_only) as database:
        assert type(database) is sqlite3.Connection
        assert require_prepared_git_connection(database, path) is None
        assert database.execute("SELECT * FROM records").fetchall() == [("A",)]
        assert database.execute("PRAGMA foreign_keys").fetchone() == (1,)
        if read_only:
            assert database.execute("PRAGMA query_only").fetchone() == (1,)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                database.execute("INSERT INTO records VALUES ('forbidden')")
            database.execute("PRAGMA query_only=OFF")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                database.execute("INSERT INTO records VALUES ('still forbidden')")
        assert database.total_changes == 0
    _closed(database)
    assert (path.read_bytes(), set(tmp_path.iterdir())) == before


def test_read_only_reuses_original_port_and_reads_committed_wal(tmp_path, monkeypatch):
    path = _database(tmp_path / "git-delivery.db")
    readonly = connection.readonly_database
    issued = []

    def open_readonly(original_path):
        assert original_path == path
        database = readonly(original_path)
        issued.append(database)
        return database

    monkeypatch.setattr(connection, "readonly_database", open_readonly)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO records VALUES ('WAL')")
        names = set(tmp_path.iterdir())
        before = {entry: entry.read_bytes() for entry in names if not entry.name.endswith("-shm")}
        with open_prepared_git_connection(path, read_only=True) as database:
            assert issued == [database]
            require_prepared_git_connection(database, path)
            assert database.execute("SELECT * FROM records").fetchall() == [("A",), ("WAL",)]
            assert database.total_changes == 0
        # 原WAL只读端口可更新已有shm读标记，但不能新增文件或修改DB/WAL正文。
        assert set(tmp_path.iterdir()) == names
        assert {entry: entry.read_bytes() for entry in before} == before


@pytest.mark.parametrize("entry", ["open", "require"])
@pytest.mark.parametrize("kind", ["string", "traversal", "nul"])
def test_invalid_path_arguments_are_fail_closed(tmp_path, entry, kind):
    path = _database(tmp_path / "git-delivery.db")
    if kind == "string":
        invalid = str(path)
    elif kind == "traversal":
        directory = tmp_path / "nested"
        directory.mkdir()
        invalid = directory / ".." / path.name
    else:
        invalid = Path(str(path) + "\0")
    if entry == "open":
        with _invalid(), open_prepared_git_connection(invalid, read_only=True):
            pytest.fail("非法路径不能作为原连接来源")
    else:
        with open_prepared_git_connection(path, read_only=True) as database:
            with _invalid():
                require_prepared_git_connection(database, invalid)


@pytest.mark.parametrize("read_only", [None, 0, 1, "false"])
def test_read_only_flag_must_be_boolean(tmp_path, read_only):
    path = _database(tmp_path / "git-delivery.db")
    with _invalid(), open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("连接模式不能隐式转换")


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("missing_parent", [False, True])
def test_missing_file_or_parent_is_never_created(tmp_path, read_only, missing_parent):
    path = tmp_path / "absent.db"
    if missing_parent:
        path = tmp_path / "absent-directory" / path.name
    with _invalid(), open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("不存在的数据库不能打开")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("read_only", [False, True])
def test_parent_must_be_an_actual_directory(tmp_path, read_only):
    parent = _database(tmp_path / "parent-is-file")
    before = parent.read_bytes(), set(tmp_path.iterdir())
    with _invalid(), open_prepared_git_connection(parent / "git-delivery.db", read_only=read_only):
        pytest.fail("普通文件不能作为数据库父目录")
    assert (parent.read_bytes(), set(tmp_path.iterdir())) == before


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("kind", ["directory", "symlink", "dangling_symlink", "fifo"])
def test_non_regular_file_is_rejected_without_changes(tmp_path, read_only, kind):
    path = tmp_path / "invalid.db"
    target = _database(tmp_path / "target.db")
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling_symlink":
        path.symlink_to(tmp_path / "missing.db")
    else:
        os.mkfifo(path)
    before = target.read_bytes(), set(tmp_path.iterdir()), path.lstat()
    with _invalid(), open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("非普通文件不能打开")
    assert (target.read_bytes(), set(tmp_path.iterdir()), path.lstat()) == before


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("ancestor", [False, True])
def test_symbolic_parent_or_ancestor_directory_is_rejected(tmp_path, read_only, ancestor):
    real = tmp_path / "real"
    real.mkdir()
    parent = real / "nested" if ancestor else real
    if ancestor:
        parent.mkdir()
    original = _database(parent / "git-delivery.db")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    path = alias / "nested" / original.name if ancestor else alias / original.name
    before = original.read_bytes(), set(tmp_path.iterdir())
    with _invalid(), open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("带符号链接的父目录不能打开")
    assert (original.read_bytes(), set(tmp_path.iterdir())) == before


def test_raw_sqlite_connection_cannot_claim_existing_or_self_created_source(tmp_path):
    existing = _database(tmp_path / "existing.db")
    for path in (existing, tmp_path / "caller-created.db"):
        with closing(sqlite3.connect(path)) as raw:
            with _invalid():
                require_prepared_git_connection(raw, path)
    with open_prepared_git_connection(existing, read_only=True) as original:
        with closing(sqlite3.connect(existing)) as raw:
            with _invalid():
                require_prepared_git_connection(raw, existing)
        with _invalid():
            require_prepared_git_connection(
                SimpleNamespace(database=original, path=existing), existing
            )
        require_prepared_git_connection(original, existing)


def test_factory_rejects_readonly_port_returning_another_database(tmp_path, monkeypatch):
    path = _database(tmp_path / "original.db")
    other = _database(tmp_path / "other.db")
    database = connection.readonly_database(other)
    monkeypatch.setattr(connection, "readonly_database", lambda path: database)
    with _invalid(), open_prepared_git_connection(path, read_only=True):
        pytest.fail("原端口返回的连接必须指向传入路径")
    _closed(database)
    with _invalid():
        require_prepared_git_connection(database, path)


@pytest.mark.parametrize("alias", [False, True])
def test_wrong_path_is_rejected_even_for_same_inode(tmp_path, alias):
    path = _database(tmp_path / "original.db")
    other = tmp_path / "other.db"
    if alias:
        os.link(path, other)
        assert path.stat().st_ino == other.stat().st_ino
    else:
        _database(other)
    with open_prepared_git_connection(path, read_only=True) as database:
        with _invalid():
            require_prepared_git_connection(database, other)
        require_prepared_git_connection(database, path)


def test_relative_path_is_bound_to_original_absolute_path(tmp_path, monkeypatch):
    path = _database(tmp_path / "original.db")
    monkeypatch.chdir(tmp_path)
    with open_prepared_git_connection(Path(path.name), read_only=True) as database:
        require_prepared_git_connection(database, path)
        require_prepared_git_connection(database, Path(path.name))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        with _invalid():
            require_prepared_git_connection(database, Path(path.name))


@pytest.mark.parametrize("read_only", [False, True])
def test_opened_a_connection_rejects_path_replaced_with_b(tmp_path, read_only):
    path = _database(tmp_path / "git-delivery.db", "A")
    replacement = _database(tmp_path / "replacement.db", "B")
    with open_prepared_git_connection(path, read_only=read_only) as database:
        assert database.execute("SELECT * FROM records").fetchall() == [("A",)]
        path.rename(tmp_path / "original-a.db")
        replacement.rename(path)
        before = path.read_bytes(), set(tmp_path.iterdir()), database.total_changes
        # SQLite仍报告同一路径、实际仍读取A；不能以PRAGMA路径充当物理来源证明。
        assert database.execute("PRAGMA database_list").fetchall() == [(0, "main", str(path))]
        assert database.execute("SELECT * FROM records").fetchall() == [("A",)]
        with _invalid():
            require_prepared_git_connection(database, path)
        assert (path.read_bytes(), set(tmp_path.iterdir()), database.total_changes) == before
        with open_prepared_git_connection(path, read_only=True) as fresh:
            require_prepared_git_connection(fresh, path)
            assert fresh.execute("SELECT * FROM records").fetchall() == [("B",)]


@pytest.mark.parametrize("damage", ["missing", "symlink", "directory"])
def test_active_connection_rejects_missing_or_non_regular_path(tmp_path, damage):
    path = _database(tmp_path / "git-delivery.db")
    with open_prepared_git_connection(path, read_only=True) as database:
        original = tmp_path / "original.db"
        path.rename(original)
        if damage == "symlink":
            path.symlink_to(original)
        elif damage == "directory":
            path.mkdir()
        with _invalid():
            require_prepared_git_connection(database, path)


def test_replaced_parent_is_rejected_even_when_database_inode_is_preserved(tmp_path):
    parent = tmp_path / "git-delivery"
    parent.mkdir()
    path = _database(parent / "git-delivery.db")
    before = path.stat()
    with open_prepared_git_connection(path, read_only=True) as database:
        retired = tmp_path / "retired"
        parent.rename(retired)
        parent.mkdir()
        (retired / path.name).rename(path)
        assert (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)
        with _invalid():
            require_prepared_git_connection(database, path)


def test_active_context_rejects_parent_replaced_by_symbolic_directory(tmp_path):
    parent = tmp_path / "git-delivery"
    parent.mkdir()
    path = _database(parent / "git-delivery.db")
    with open_prepared_git_connection(path, read_only=True) as database:
        retired = tmp_path / "retired"
        parent.rename(retired)
        parent.symlink_to(retired, target_is_directory=True)
        with _invalid():
            require_prepared_git_connection(database, path)


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("replace_after_open", [False, True])
def test_open_before_after_pin_and_non_creating_uri_close_failures(
    tmp_path, monkeypatch, read_only, replace_after_open
):
    path = _database(tmp_path / "git-delivery.db")
    replacement = _database(tmp_path / "replacement.db", "B")
    replacement_bytes = replacement.read_bytes()
    connect = sqlite3.connect
    opened = []

    def swap(*args, **kwargs):
        if replace_after_open:
            database = connect(*args, **kwargs)
            opened.append(database)
            path.rename(tmp_path / "original.db")
            replacement.rename(path)
            return database
        path.unlink()
        return connect(*args, **kwargs)

    monkeypatch.setattr(connection.sqlite3, "connect", swap)
    with _invalid(), open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("打开期间物理对象变化不能登记来源")
    if replace_after_open:
        assert path.read_bytes() == replacement_bytes
        assert len(opened) == 1
        _closed(opened[0])
        with _invalid():
            require_prepared_git_connection(opened[0], path)
    else:
        assert not path.exists()


@pytest.mark.parametrize("read_only", [False, True])
def test_closed_connection_and_expired_context_are_rejected(tmp_path, read_only):
    path = _database(tmp_path / "git-delivery.db")
    with open_prepared_git_connection(path, read_only=read_only) as closed:
        closed.close()
        with _invalid():
            require_prepared_git_connection(closed, path)
    with open_prepared_git_connection(path, read_only=read_only) as expired:
        require_prepared_git_connection(expired, path)
    _closed(expired)
    with _invalid():
        require_prepared_git_connection(expired, path)
    with open_prepared_git_connection(path, read_only=True) as fresh:
        with _invalid():
            require_prepared_git_connection(expired, path)
        require_prepared_git_connection(fresh, path)
    assert not connection._owned.connections


def test_nested_contexts_and_thread_local_registration_are_independent(tmp_path):
    path = _database(tmp_path / "git-delivery.db")
    with open_prepared_git_connection(path, read_only=True) as first:
        with open_prepared_git_connection(path, read_only=True) as second:
            assert first is not second
            require_prepared_git_connection(first, path)
            require_prepared_git_connection(second, path)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(require_prepared_git_connection, first, path)
                with _invalid():
                    future.result()
        with _invalid():
            require_prepared_git_connection(second, path)
        require_prepared_git_connection(first, path)


@pytest.mark.parametrize("entry", ["open", "require"])
@pytest.mark.parametrize("read_only", [False, True])
def test_last_physical_checkpoint_cannot_leave_closed_connection_valid(
    tmp_path, monkeypatch, entry, read_only
):
    path = _database(tmp_path / "git-delivery.db")
    connect = sqlite3.connect
    opened = []

    def record(*args, **kwargs):
        database = connect(*args, **kwargs)
        opened.append(database)
        return database

    monkeypatch.setattr(connection.sqlite3, "connect", record)
    calls = []
    with open_prepared_git_connection(
        path,
        read_only=read_only,
        checkpoint=lambda: calls.append(None) if entry == "open" else None,
    ) as database:
        if entry == "require":
            require_prepared_git_connection(database, path, checkpoint=lambda: calls.append(None))
    last_physical_step = len(calls) - int(entry == "open")
    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == last_physical_step:
            opened[-1].close()

    with _invalid():
        if entry == "open":
            with open_prepared_git_connection(path, read_only=read_only, checkpoint=checkpoint):
                pytest.fail("文件观察期间关闭的原连接不能登记")
        else:
            with open_prepared_git_connection(path, read_only=read_only) as original:
                require_prepared_git_connection(original, path, checkpoint=checkpoint)
    assert count == last_physical_step
    assert not connection._owned.connections
    for database in opened:
        _closed(database)


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_transaction_is_caller_owned_and_context_never_commits(tmp_path, commit, fail):
    path = _database(tmp_path / "git-delivery.db")
    failure = KernelError("caller_failure", "调用方事务失败")
    statements = []

    def operate():
        with open_prepared_git_connection(path, read_only=False) as database:
            database.set_trace_callback(statements.append)
            database.execute("BEGIN IMMEDIATE")
            database.execute("INSERT INTO records VALUES ('pending')")
            changes = database.total_changes
            require_prepared_git_connection(database, path)
            assert database.in_transaction and database.total_changes == changes
            if commit:
                database.commit()
            if fail:
                raise failure
        return database

    if fail:
        with pytest.raises(KernelError) as caught:
            operate()
        assert caught.value is failure
    else:
        _closed(operate())
    assert statements.count("COMMIT") == int(commit)
    with open_prepared_git_connection(path, read_only=True) as reader:
        assert reader.execute("SELECT * FROM records").fetchall() == (
            [("A",), ("pending",)] if commit else [("A",)]
        )
    assert not connection._owned.connections


@pytest.mark.parametrize("entry", ["open", "require"])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize(
    "failure_type",
    [KernelError, OSError, sqlite3.OperationalError, TimeoutError, asyncio.CancelledError],
)
def test_every_checkpoint_preserves_original_exception_and_cleans_up(
    tmp_path, monkeypatch, entry, read_only, failure_type
):
    path = _database(tmp_path / "git-delivery.db")
    before = path.read_bytes(), set(tmp_path.iterdir())
    connect = sqlite3.connect
    opened = []

    def record(*args, **kwargs):
        database = connect(*args, **kwargs)
        opened.append(database)
        return database

    monkeypatch.setattr(connection.sqlite3, "connect", record)
    calls = []
    with open_prepared_git_connection(
        path,
        read_only=read_only,
        checkpoint=lambda: calls.append(None) if entry == "open" else None,
    ) as database:
        if entry == "require":
            require_prepared_git_connection(database, path, checkpoint=lambda: calls.append(None))
    assert len(calls) >= 4

    for position in range(1, len(calls) + 1):
        failure = (
            KernelError("original_control_failure", "原控制失败")
            if failure_type is KernelError
            else failure_type("原控制失败")
        )
        count = 0

        def checkpoint(stop=position, error=failure):
            nonlocal count
            count += 1
            if count == stop:
                raise error

        with pytest.raises(failure_type) as caught:
            if entry == "open":
                with open_prepared_git_connection(path, read_only=read_only, checkpoint=checkpoint):
                    pass
            else:
                with open_prepared_git_connection(path, read_only=read_only) as original:
                    original.execute("BEGIN")
                    try:
                        require_prepared_git_connection(original, path, checkpoint=checkpoint)
                    finally:
                        assert original.in_transaction and original.total_changes == 0
        assert caught.value is failure
        assert count == position
        assert not connection._owned.connections
        for database in opened:
            _closed(database)
    assert (path.read_bytes(), set(tmp_path.iterdir())) == before
