"""真实SQLite同读版本、原认证、失败传播与只读边界；不赋予执行授权。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import PurePosixPath, PureWindowsPath
from time import monotonic
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    Budget,
    EventDraft,
    ItemStarted,
    TextContent,
    ThreadArchived,
    ThreadCreated,
    TurnStarted,
)
from harnessix.agent.reducer import replay
from harnessix.product_config.contracts import SecretReference
from harnessix.product_config.state_backup_validation import _VerificationOnlyScope
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session import sqlite as sqlite_module
from harnessix.session import sqlite_history as history_module
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.session.store_publication import SessionPublicationBinding

KEY = bytes(range(32))


@contextmanager
def binding(store_id=None, key_id=None, *, key=KEY):
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("fixture", "1", "KEY"),),
        environment={"KEY": "history-fixture-secret/+19"},
    )
    with SecretPublicationScope((SecretReference(name="fixture", version="1"),), provider) as scope:
        proof = SessionPublicationBinding(store_id or uuid4(), key_id or uuid4(), key, scope)
        try:
            yield proof
        finally:
            proof.close()


async def seed(path, proof):
    store = SQLiteSessionStore(path, publication=proof)
    await store.initialize()
    tid = uuid4()
    thread = await store.append(
        tid,
        [EventDraft(payload=ThreadCreated(workspace=(path.parent / "workspace").as_posix()))],
        expected_sequence=0,
    )
    return store, tid, thread


async def read(store, tid, **controls):
    return await store.authenticated_thread_history(
        tid,
        cancel=controls.pop("cancel", CancelToken()),
        deadline=controls.pop("deadline", monotonic() + 10.0),
        **controls,
    )


def business_rows(path):
    """完整业务行作前后比对；不把WAL/SHM辅助字节当作业务写入。"""
    with closing(sqlite3.connect(path)) as db, db:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for (name,) in tables
        }


@pytest.mark.parametrize("fail_first", [False, True], ids=["normal", "sql_error"])
def test_business_rows_probe_closes_connection(tmp_path, monkeypatch, fail_first):
    path = tmp_path / "probe.db"
    with closing(sqlite3.connect(path)) as setup, setup:
        setup.execute("CREATE TABLE fixture_rows (value TEXT)")
        setup.execute("INSERT INTO fixture_rows VALUES ('fixture')")

    captured = []

    class CapturedConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if self.fail_first:
                self.fail_first = False
                return super().execute("SELECT * FROM missing_probe_table")
            return super().execute(statement, parameters)

    original_connect = sqlite3.connect

    def capture_connect(*args, **kwargs):
        connection = original_connect(*args, factory=CapturedConnection, **kwargs)
        connection.fail_first = fail_first
        captured.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture_connect)
    if fail_first:
        with pytest.raises(sqlite3.OperationalError, match="no such table") as caught:
            business_rows(path)
        assert "missing_probe_table" in str(caught.value)
    else:
        assert business_rows(path) == {"fixture_rows": [("fixture",)]}

    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


@pytest.mark.parametrize("fail_first", [False, True], ids=["normal", "sql_error"])
def test_business_probe_fixture_closes_setup_connection_on_sql_error(
    tmp_path, monkeypatch, fail_first
):
    """初始化真实SQL失败也须关闭；强引用阻止GC代替夹具结算连接。"""
    captured = []
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_authorizer(
            lambda action, table, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table == "fixture_rows"
                else sqlite3.SQLITE_OK
            )
        )
        captured.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            test_business_rows_probe_closes_connection(tmp_path, monkeypatch, fail_first)
        assert len(captured) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")
    finally:
        for connection in captured:
            connection.close()


async def test_complete_history_is_original_authenticated_replay_without_business_writes(tmp_path):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "state.db", proof)
        before = business_rows(store.path)
        frame = await read(store, tid)
        assert type(frame) is AuthenticatedThreadHistory
        assert frame.thread == expected == replay(frame.events)
        assert type(frame.events) is tuple
        assert tuple(e.sequence for e in frame.events) == (1,)
        assert not any(hasattr(frame, field) for field in ("key", "execute", "issue", "resume"))
        assert business_rows(store.path) == before


async def test_missing_database_is_not_created(tmp_path):
    with binding() as proof:
        path = tmp_path / "missing.db"
        store = SQLiteSessionStore(path, publication=proof)
        with pytest.raises(KernelError) as caught:
            await read(store, uuid4())
        assert caught.value.code == "storage_unavailable"
        assert not path.exists()


async def test_checkpoint_original_exception_is_not_replaced(tmp_path):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        original = RuntimeError("fixture-owner-unavailable")

        def fail():
            raise original

        with pytest.raises(RuntimeError) as caught:
            await read(store, tid, checkpoint=fail)
        assert caught.value is original


@pytest.mark.parametrize("kind", [RuntimeError, OSError, sqlite3.OperationalError])
@pytest.mark.parametrize("point", ["entry", "inside", "after_close"])
async def test_owner_exception_identity_is_preserved_outside_storage_classification(
    tmp_path, monkeypatch, kind, point
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        original = kind("fixture-original-owner-error")
        connection, enabled, closed = store._connection, point == "entry", []

        @asynccontextmanager
        async def observe(**kwargs):
            nonlocal enabled
            try:
                async with connection(**kwargs) as db:
                    if point == "inside":
                        enabled = True
                    yield db
            finally:
                closed.append(db._connection is None)
                if point == "after_close":
                    enabled = True

        def owner_checkpoint():
            if enabled:
                raise original

        monkeypatch.setattr(store, "_connection", observe)
        before = business_rows(store.path)
        with pytest.raises(kind) as caught:
            await read(store, tid, checkpoint=owner_checkpoint)
        assert caught.value is original
        assert closed == ([] if point == "entry" else [True])
        assert business_rows(store.path) == before


@asynccontextmanager
async def deny_driver_operation(connection, observations, *, rollback_only=False):
    """真实驱动授权拒绝，不用替换rollback方法模拟清理异常。"""
    database = None
    try:
        async with connection(authenticate=False, read_only=True) as database:

            def authorizer(action, first, _second, _database, _source):
                denied = (
                    action == sqlite3.SQLITE_TRANSACTION and first == "ROLLBACK"
                    if rollback_only
                    else action == sqlite3.SQLITE_READ
                )
                if denied:
                    observations["denials"] += 1
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            await database.set_authorizer(authorizer)
            yield database
    finally:
        observations["closed"] = database is not None and database._connection is None


@pytest.mark.parametrize("kind", [OSError, sqlite3.OperationalError, RuntimeError])
async def test_actual_rollback_failure_takes_priority_over_owner_error(tmp_path, monkeypatch, kind):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        original, connection = kind("fixture-original-owner-error"), store._connection
        before = business_rows(store.path)
        observations = {"denials": 0, "closed": False}

        @asynccontextmanager
        async def instrument(**kwargs):
            assert kwargs == {"authenticate": False, "read_only": True}
            async with deny_driver_operation(connection, observations, rollback_only=True) as db:
                yield db

        calls = 0

        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise original

        monkeypatch.setattr(store, "_connection", instrument)
        with pytest.raises(KernelError) as caught:
            await read(store, tid, checkpoint=checkpoint)
        assert caught.value.code == "storage_unavailable"
        assert calls == 3 and observations == {"denials": 1, "closed": True}
        assert business_rows(store.path) == before


async def test_actual_driver_failure_without_owner_error_keeps_storage_classification(
    tmp_path, monkeypatch
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        connection, before = store._connection, business_rows(store.path)
        observations = {"denials": 0, "closed": False}

        @asynccontextmanager
        async def instrument(**kwargs):
            assert kwargs == {"authenticate": False, "read_only": True}
            async with deny_driver_operation(connection, observations) as db:
                yield db

        monkeypatch.setattr(store, "_connection", instrument)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == "storage_unavailable"
        assert observations["denials"] >= 1 and observations["closed"]
        assert business_rows(store.path) == before


async def test_parent_cancel_during_owner_error_preserves_cancellation_priority(
    tmp_path, monkeypatch
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        connection, before, closed = store._connection, business_rows(store.path), []

        @asynccontextmanager
        async def instrument(**kwargs):
            database = None
            try:
                async with connection(**kwargs) as database:
                    yield database
            finally:
                closed.append(database is not None and database._connection is None)

        calls = 0

        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 3:
                asyncio.current_task().cancel()
                raise OSError("fixture-original-owner-error")

        monkeypatch.setattr(store, "_connection", instrument)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(read(store, tid, checkpoint=checkpoint))
        assert calls == 3 and closed == [True]
        assert business_rows(store.path) == before


async def test_original_history_reopens_with_verification_only_scope(tmp_path):
    sid, kid, path = uuid4(), uuid4(), tmp_path / "state.db"
    with binding(sid, kid) as proof:
        store, tid, expected = await seed(path, proof)
        before = business_rows(path)
    proof = SessionPublicationBinding(sid, kid, KEY, _VerificationOnlyScope())
    try:
        store = SQLiteSessionStore(path, publication=proof)
        frame = await read(store, tid)
        assert frame.thread == expected == replay(frame.events)
        assert business_rows(path) == before
    finally:
        proof.close()


@pytest.mark.parametrize("wrong", ["none", "key", "key_id", "store_id", "closed"])
async def test_unavailable_or_wrong_original_binding_never_returns_history(tmp_path, wrong):
    sid, kid, path = uuid4(), uuid4(), tmp_path / "state.db"
    with binding(sid, kid) as proof:
        _, tid, _ = await seed(path, proof)
    with binding(
        uuid4() if wrong == "store_id" else sid,
        uuid4() if wrong == "key_id" else kid,
        key=bytes(reversed(KEY)) if wrong == "key" else KEY,
    ) as proof:
        store = SQLiteSessionStore(path, publication=None if wrong == "none" else proof)
        if wrong == "closed":
            proof.close()
        before = business_rows(path)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == (
            "publication_key_unavailable" if wrong == "closed" else "publication_history_unproven"
        )
        assert business_rows(path) == before


async def test_unknown_thread_does_not_initialize_or_append(tmp_path):
    with binding() as proof:
        store, _, _ = await seed(tmp_path / "state.db", proof)
        before = business_rows(store.path)
        with pytest.raises(KernelError) as caught:
            await read(store, uuid4())
        assert caught.value.code == "thread_not_found"
        assert business_rows(store.path) == before


@pytest.mark.parametrize(
    "argument,value",
    [
        ("deadline", float("nan")),
        ("deadline", float("inf")),
        ("deadline", float("-inf")),
        ("deadline", True),
        ("deadline", 5),
        ("cancel", None),
        ("checkpoint", True),
        ("thread_id", "not-a-uuid"),
    ],
)
async def test_invalid_original_controls_fail_before_open(tmp_path, argument, value, monkeypatch):
    with binding() as proof:
        store = SQLiteSessionStore(tmp_path / "not-created.db", publication=proof)

        def forbidden(**kwargs):
            pytest.fail("无效输入不应打开连接")

        monkeypatch.setattr(store, "_connection", forbidden)
        tid = value if argument == "thread_id" else uuid4()
        controls = {} if argument == "thread_id" else {argument: value}
        with pytest.raises(KernelError) as caught:
            await read(store, tid, **controls)
        assert caught.value.code == "invalid_history_request"
        assert not store.path.exists()


async def test_legitimate_writer_between_projection_and_events_preserves_one_wal_version(
    tmp_path, monkeypatch
):
    with binding() as proof:
        store, tid, version_one = await seed(tmp_path / "state.db", proof)
        writer = SQLiteSessionStore(store.path, publication=proof)
        original = store._snapshot
        version_two = None

        async def interleave(database, thread_id):
            nonlocal version_two
            snapshot = await original(database, thread_id)
            if version_two is None:
                version_two = await writer.append(
                    tid, [EventDraft(payload=ThreadArchived(reason="fixture"))], expected_sequence=1
                )
            return snapshot

        monkeypatch.setattr(store, "_snapshot", interleave)
        frame = await read(store, tid)
        assert frame.thread == version_one
        assert len(frame.events) == 1
        assert version_two is not None and version_two.sequence == 2
        frame = await read(store, tid)
        assert frame.thread == version_two == replay(frame.events)
        assert len(frame.events) == 2


async def test_middle_event_mac_is_checked_before_its_json_parser(tmp_path, monkeypatch):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        turn_id = uuid4()
        await store.append(
            tid,
            [
                EventDraft(
                    turn_id=turn_id,
                    payload=TurnStarted(
                        request_id="fixture", request_fingerprint="1" * 64, budget=Budget()
                    ),
                ),
                EventDraft(
                    turn_id=turn_id,
                    payload=ItemStarted(
                        item_id=uuid4(), content=TextContent(kind="user_message", text="fixture")
                    ),
                ),
            ],
            expected_sequence=1,
        )
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("UPDATE agent_events SET event_json='not-json' WHERE sequence=2")
        before = business_rows(store.path)
        original, parsed = AgentEvent.model_validate_json, []

        def parser(body, *args, **kwargs):
            assert body != "not-json", "损坏MAC对应正文不得进入解析器"
            parsed.append(body)
            return original(body, *args, **kwargs)

        monkeypatch.setattr(AgentEvent, "model_validate_json", parser)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == "publication_history_unproven"
        assert len(parsed) == 1
        assert business_rows(store.path) == before


@pytest.mark.parametrize("valid_seal", [False, True])
async def test_snapshot_replacement_even_with_valid_seal_cannot_disagree_with_replay(
    tmp_path, valid_seal
):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "state.db", proof)
        changed = expected.model_copy(update={"workspace": (tmp_path / "other").as_posix()})
        body = changed.model_dump_json()
        with closing(sqlite3.connect(store.path)) as db, db:
            if valid_seal:
                raw = db.execute("SELECT seal FROM agent_projection_publications").fetchone()[0]
                checkpoint = proof.verify_projection(raw, tid)
                seal = proof.projection(
                    changed, body, checkpoint.prefix_sha256, checkpoint.history_bytes
                )
                db.execute("UPDATE agent_projection_publications SET seal=?", (seal,))
            db.execute(
                "UPDATE agent_threads SET snapshot_json=?, snapshot_sha256=?",
                (body, hashlib.sha256(body.encode()).hexdigest()),
            )
        before = business_rows(store.path)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == "publication_history_unproven"
        assert business_rows(store.path) == before


@pytest.mark.parametrize(
    "table",
    ["agent_publication_store", "agent_projection_publications", "agent_event_publications"],
)
async def test_original_proof_is_not_reissued_when_missing(tmp_path, table):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("DELETE FROM " + table)
        before = business_rows(store.path)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == "publication_history_unproven"
        assert business_rows(store.path) == before


@pytest.mark.parametrize("stop", ["cancel", "deadline", "close", "replace"])
@pytest.mark.parametrize("point", ["entry", "after_read", "after_close"])
async def test_normal_checkpoint_return_cannot_publish_after_original_stop(
    tmp_path, monkeypatch, stop, point
):
    with binding() as proof, binding() as replacement:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        token, triggered, enabled = CancelToken(), False, point == "entry"
        original_events, original_connection = (
            history_module.authenticated_events,
            store._connection,
        )

        async def events(*args, **kwargs):
            nonlocal enabled
            result = await original_events(*args, **kwargs)
            if point == "after_read":
                enabled = True
            return result

        @asynccontextmanager
        async def connection(**kwargs):
            nonlocal enabled
            async with original_connection(**kwargs) as database:
                yield database
            if point == "after_close":
                enabled = True

        def checkpoint():
            nonlocal triggered
            if not enabled or triggered:
                return
            triggered = True
            if stop == "cancel":
                token.cancel()
            elif stop == "close":
                proof.close()
            elif stop == "replace":
                store._publication = replacement
            else:
                monkeypatch.setattr(history_module, "monotonic", lambda: float("inf"))

        monkeypatch.setattr(history_module, "authenticated_events", events)
        monkeypatch.setattr(store, "_connection", connection)
        before = business_rows(store.path)
        exception = TurnCancelled if stop == "cancel" else KernelError
        with pytest.raises(exception) as caught:
            await read(store, tid, cancel=token, checkpoint=checkpoint)
        assert triggered
        if stop != "cancel":
            assert (
                caught.value.code
                == {
                    "deadline": "publication_history_timeout",
                    "close": "publication_key_unavailable",
                    "replace": "publication_history_unproven",
                }[stop]
            )
        assert business_rows(store.path) == before


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_preexisting_stop_never_opens_database(tmp_path, monkeypatch, stop):
    with binding() as proof:
        store = SQLiteSessionStore(tmp_path / "not-created.db", publication=proof)
        token = CancelToken()
        deadline = monotonic() + 10.0
        if stop == "cancel":
            token.cancel()
        else:
            deadline = monotonic() - 1.0

        def forbidden(**kwargs):
            pytest.fail("入口停止不应打开连接")

        monkeypatch.setattr(store, "_connection", forbidden)
        with pytest.raises(TurnCancelled if stop == "cancel" else KernelError):
            await read(store, uuid4(), cancel=token, deadline=deadline)
        assert not store.path.exists()


@pytest.mark.parametrize(
    "path,expected_uri",
    [
        (
            PurePosixPath("/private/tmp/uri ?#% 中文.db"),
            "file:///private/tmp/uri%20%3F%23%25%20%E4%B8%AD%E6%96%87.db",
        ),
        (
            PureWindowsPath("C:/private/tmp/uri ?#% 中文.db"),
            "file:///C:/private/tmp/uri%20%3F%23%25%20%E4%B8%AD%E6%96%87.db",
        ),
    ],
    ids=["pure-posix", "pure-windows"],
)
async def test_readonly_uri_escapes_pure_path_without_opening_database(
    monkeypatch, path, expected_uri
):
    calls = []
    original = RuntimeError("fixture-uri-capture")

    def capture(database, **kwargs):
        calls.append((database, kwargs))
        raise original

    monkeypatch.setattr(sqlite_module.aiosqlite, "connect", capture)
    with pytest.raises(RuntimeError) as caught:
        async with sqlite_module._session_connection(path, read_only=True):
            pytest.fail("URI捕获后不得继续打开数据库")

    assert caught.value is original
    assert calls == [(expected_uri + "?mode=ro", {"uri": True})]


async def test_readonly_connection_observes_wal_and_refuses_business_dml(tmp_path, monkeypatch):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "uri #% 中文.db", proof)
        original, statements, closed = store._connection, [], []

        @asynccontextmanager
        async def observe(**kwargs):
            assert kwargs == {"authenticate": False, "read_only": True}
            async with original(**kwargs) as db:
                await db.set_trace_callback(statements.append)
                cursor = await db.execute("PRAGMA query_only")
                assert (await cursor.fetchone())[0] == 1
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    await db.execute("DELETE FROM agent_events")
                # sqlite3在失败DML前可能已隐式BEGIN；只结算测试自己的拒绝探针。
                await db.rollback()
                statements.clear()
                yield db
            closed.append(db._connection is None)

        monkeypatch.setattr(store, "_connection", observe)
        before = business_rows(store.path)
        assert (await read(store, tid)).thread == expected
        assert closed == [True]
        actual = [s for s in statements if not s.startswith("DELETE")]
        assert len([s for s in actual if s == "BEGIN"]) == 1
        assert not any(s.startswith(("INSERT", "UPDATE", "CREATE", "COMMIT")) for s in actual)
        assert business_rows(store.path) == before


@pytest.mark.parametrize("stop", ["deadline", "cancel", "parent"])
async def test_real_sqlite_vm_stop_propagates_original_cause_and_settles_connection(
    tmp_path, monkeypatch, stop
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        token, started, release = CancelToken(), threading.Event(), threading.Event()
        original, original_snapshot = store._connection, store._snapshot
        closed, owner_threads = [], []

        @asynccontextmanager
        async def connection(**kwargs):
            try:
                async with original(**kwargs) as db:
                    yield db
            finally:
                closed.append(db._connection is None)

        def barrier():
            started.set()
            assert release.wait(5.0), "测试必须释放真实SQLite工作线程"
            return 1

        async def slow(database, thread_id):
            await database.create_function("fixture_barrier", 0, barrier)
            await database.execute(
                "WITH RECURSIVE items(x) AS (SELECT fixture_barrier() UNION ALL "
                "SELECT x+1 FROM items WHERE x<1000000) SELECT sum(x) FROM items"
            )
            return await original_snapshot(database, thread_id)

        monkeypatch.setattr(store, "_connection", connection)
        monkeypatch.setattr(store, "_snapshot", slow)
        before = business_rows(store.path)
        task = asyncio.create_task(
            read(
                store,
                tid,
                cancel=token,
                checkpoint=lambda: owner_threads.append(threading.get_ident()),
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 3.0)
            if stop == "cancel":
                token.cancel()
            elif stop == "deadline":
                monkeypatch.setattr(history_module, "monotonic", lambda: float("inf"))
            else:
                task.cancel()
            release.set()
            expected = {
                "cancel": TurnCancelled,
                "deadline": KernelError,
                "parent": asyncio.CancelledError,
            }[stop]
            with pytest.raises(expected) as caught:
                await task
            if stop == "deadline":
                assert caught.value.code == "publication_history_timeout"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert owner_threads and set(owner_threads) == {threading.get_ident()}
        assert closed == [True]
        assert business_rows(store.path) == before
        store.path.unlink()
        assert not store.path.exists()


@pytest.mark.parametrize("limit", ["events", "history_bytes", "projection_bytes", "event_timeout"])
async def test_original_full_history_limits_are_not_bypassed(tmp_path, monkeypatch, limit):
    from harnessix.session import sqlite_publication

    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        before = business_rows(store.path)
        if limit == "event_timeout":
            ticks = iter((0.0, 11.0))
            monkeypatch.setattr(sqlite_publication, "monotonic", lambda: next(ticks))
        else:
            field = {
                "events": "MAX_HISTORY_EVENTS",
                "history_bytes": "MAX_HISTORY_BYTES",
                "projection_bytes": "MAX_PROJECTION_BYTES",
            }[limit]
            monkeypatch.setattr(sqlite_publication, field, 0)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert (
            caught.value.code
            == {
                "events": "publication_history_limit",
                "history_bytes": "publication_history_limit",
                "projection_bytes": "publication_history_unproven",
                "event_timeout": "publication_history_timeout",
            }[limit]
        )
        assert business_rows(store.path) == before


async def test_real_runtime_and_fork_history_keep_original_metadata_semantics(tmp_path):
    from harnessix.agent.lifecycle import prepare_fork_snapshot
    from harnessix.agent.models import ThreadForked
    from harnessix.agent.runtime import AgentRuntime
    from harnessix.context.tool_result_contracts import ToolResultViewPolicy
    from harnessix.models.contracts import (
        ResponseCompleted,
        ResponseStarted,
        TextCompleted,
        TextDelta,
        TextStarted,
    )
    from harnessix.models.scripted import ScriptedProvider

    with binding() as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="fixture"),
                    TextStarted(content_id="answer"),
                    TextDelta(content_id="answer", delta="完成"),
                    TextCompleted(content_id="answer", text="完成"),
                    ResponseCompleted(),
                ]
            ]
        )
        async with AgentRuntime(
            store, provider, public_output_protection=proof._events._protection
        ) as runtime:
            source = await runtime.create_thread(tmp_path.as_posix())
            turn = await runtime.run_turn(source.thread_id, "任务", request_id="history")
            assert turn.status.value == "completed"
            parent = await read(store, source.thread_id)
            assert parent.thread == replay(parent.events)
            snapshot = prepare_fork_snapshot(
                parent.thread,
                request_id="fork",
                through_turn_id=None,
                policy=ToolResultViewPolicy(),
            ).snapshot
            child = await store.fork(
                source.thread_id,
                uuid4(),
                EventDraft(payload=ThreadForked(workspace=source.workspace, snapshot=snapshot)),
                expected_source_sequence=parent.thread.sequence,
            )
            frame = await read(store, child.thread_id)
            assert frame.thread == child == replay(frame.events)
            assert frame.thread.fork_snapshot == snapshot
            assert len(frame.events) == 1
            assert len(provider.requests) == 1
