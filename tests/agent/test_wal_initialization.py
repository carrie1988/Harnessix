import asyncio
import sqlite3

import aiosqlite
import pytest

from harnessix.agent.errors import KernelError
from harnessix.session import sqlite as session
from harnessix.session.sqlite import SQLiteSessionStore

WAL = "PRAGMA journal_mode = WAL"
CANARY = "不得传播原始存储异常 SECRET-CANARY"


def busy_error(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError(CANARY)
    error.sqlite_errorcode = code
    return error


def assert_initialized(path):
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == 8
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"


async def test_concurrent_first_initialization_converges(tmp_path):
    for index in range(20):
        path = tmp_path / f"{index}.db"
        await asyncio.gather(*(SQLiteSessionStore(path).initialize() for _ in range(4)))
        assert_initialized(path)


async def test_only_wal_transition_is_retried_not_migrations(tmp_path, monkeypatch):
    original = aiosqlite.Connection.execute
    statements = []

    async def execute(database, sql, parameters=None):
        statements.append(sql)
        if sql == WAL and statements.count(WAL) <= 2:
            raise busy_error()
        return await original(database, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    path = tmp_path / "s.db"
    await SQLiteSessionStore(path).initialize()
    assert statements.count(WAL) == 3
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert sum(s.startswith("INSERT INTO agent_migrations") for s in statements) == 8
    assert_initialized(path)


async def test_busy_deadline_is_bounded_and_does_not_hide_committed_migrations(
    tmp_path, monkeypatch
):
    original = aiosqlite.Connection.execute
    calls = 0

    async def execute(database, sql, parameters=None):
        nonlocal calls
        if sql == WAL:
            calls += 1
            raise busy_error()
        return await original(database, sql, parameters)

    path = tmp_path / "s.db"
    with monkeypatch.context() as patch:
        patch.setattr(session, "_WAL_TIMEOUT_SECONDS", 0)
        patch.setattr(aiosqlite.Connection, "execute", execute)
        with pytest.raises(KernelError) as caught:
            await SQLiteSessionStore(path).initialize()
    assert caught.value.code == "storage_busy" and caught.value.retryable and calls == 1
    assert CANARY not in str(caught.value)
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM agent_migrations").fetchone()[0] == 8
    await SQLiteSessionStore(path).initialize()
    assert_initialized(path)


@pytest.mark.parametrize(
    "code,expected",
    [(sqlite3.SQLITE_IOERR, "storage_unavailable"), (sqlite3.SQLITE_CORRUPT, "database_corrupt")],
)
async def test_non_busy_failure_is_not_retried(tmp_path, monkeypatch, code, expected):
    original = aiosqlite.Connection.execute
    calls = 0

    async def execute(database, sql, parameters=None):
        nonlocal calls
        if sql == WAL:
            calls += 1
            raise busy_error(code)
        return await original(database, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    with pytest.raises(KernelError) as caught:
        await SQLiteSessionStore(tmp_path / "s.db").initialize()
    assert caught.value.code == expected and calls == 1
    assert CANARY not in str(caught.value)


async def test_cancelled_wal_wait_closes_connections_and_can_initialize_later(
    tmp_path, monkeypatch
):
    original, original_close = aiosqlite.Connection.execute, aiosqlite.Connection.close
    entered = asyncio.Event()
    closed = []

    async def execute(database, sql, parameters=None):
        if sql == WAL:
            entered.set()
            raise busy_error()
        return await original(database, sql, parameters)

    async def close(database):
        await original_close(database)
        closed.append(database)

    path = tmp_path / "s.db"
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "execute", execute)
        patch.setattr(aiosqlite.Connection, "close", close)
        task = asyncio.create_task(SQLiteSessionStore(path).initialize())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(closed) == 2
    await SQLiteSessionStore(path).initialize()
    assert_initialized(path)


async def test_actual_sqlite_writer_contention_releases_without_replaying_migrations(
    tmp_path, monkeypatch
):
    original = aiosqlite.Connection.execute
    path = tmp_path / "s.db"
    writer = None
    blocked = asyncio.Event()
    calls = 0
    busy_count = 0

    async def execute(database, sql, parameters=None):
        nonlocal writer, calls, busy_count
        if sql == WAL:
            calls += 1
            if writer is None:
                writer = sqlite3.connect(path)
                writer.execute("BEGIN IMMEDIATE")
        try:
            return await original(database, sql, parameters)
        except sqlite3.OperationalError:
            busy_count += 1
            blocked.set()
            raise

    async def release_writer():
        await asyncio.wait_for(blocked.wait(), 5)
        assert writer is not None
        writer.rollback()

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    try:
        await asyncio.gather(SQLiteSessionStore(path).initialize(), release_writer())
    finally:
        if writer is not None:
            writer.close()
    assert calls >= 2 and busy_count >= 1
    assert_initialized(path)


async def test_journal_mode_result_must_actually_be_wal(tmp_path, monkeypatch):
    original = aiosqlite.Connection.execute

    async def execute(database, sql, parameters=None):
        if sql == WAL:
            sql = "PRAGMA journal_mode"
        return await original(database, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    with pytest.raises(KernelError, match="无法启用 WAL"):
        await SQLiteSessionStore(tmp_path / "s.db").initialize()
