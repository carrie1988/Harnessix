"""四库 monitor 的原工厂控制契约；fake token 不证明实际 FD 身份。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_link_connection as factory
from harnessix.product_config import git_prepared_link_observation as observation
from harnessix.product_config import git_prepared_native_identity as native
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window


@pytest.fixture(autouse=True)
def isolated_identity(monkeypatch):
    monkeypatch.setattr(native, "_state", "not_started")
    monkeypatch.setattr(native, "_backend", None)
    monkeypatch.setattr(native, "_connections_started", False)
    monkeypatch.setattr(factory._owned, "connections", {}, raising=False)


@pytest.fixture
def databases(tmp_path, monkeypatch):
    paths = tuple(tmp_path / f"{name}.db" for name in ("session", "audit", "plans", "core"))
    with ExitStack() as stack:
        writers = tuple(
            stack.enter_context(closing(sqlite3.connect(path, isolation_level=None)))
            for path in paths
        )
        for writer in writers:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            writer.execute("CREATE TABLE records(value)")
            writer.execute("INSERT INTO records VALUES ('WAL')")
        router = SimpleNamespace(
            _audit=SimpleNamespace(_path=paths[1], _db=writers[1]),
            _plans=SimpleNamespace(_path=paths[2], _db=writers[2]),
        )
        core = SimpleNamespace(store=SimpleNamespace(_path=paths[3], _db=writers[3]))
        artifacts = SimpleNamespace(session=SimpleNamespace(path=paths[0]))
        opened = []
        readonly = factory.readonly_database

        def open_reader(path):
            database = readonly(path)
            opened.append(database)
            return database

        monkeypatch.setattr(factory, "readonly_database", open_reader)
        yield SimpleNamespace(
            args=(router, core, artifacts), paths=paths, writers=writers, opened=opened
        )


@pytest.fixture
def fake_native(monkeypatch):
    tokens = []

    def attach(database, device, inode):
        token = SimpleNamespace(
            database=database, pin=(device, inode), check=Mock(return_value=True), release=Mock()
        )
        tokens.append(token)
        return token

    backend = SimpleNamespace(
        BridgeError=type("FakeBridgeError", (RuntimeError,), {}),
        attach_identity=Mock(side_effect=attach),
        tokens=tokens,
    )
    monkeypatch.setattr(native, "_state", "ready")
    monkeypatch.setattr(native, "_backend", backend)
    return backend


@contextmanager
def changed():
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == "git_prepared_link_changed"


def assert_closed(databases, tokens=()):
    assert not factory._owned.connections
    for database in databases.opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            database.execute("SELECT 1")
    for token in tokens:
        token.release.assert_called_once()


def test_default_legacy_four_readonly_wal_monitors_without_initialization(databases, monkeypatch):
    initialize = Mock(side_effect=AssertionError("monitor must not initialize native mode"))
    monkeypatch.setattr(native, "initialize_prepared_git_identity", initialize)
    monkeypatch.setattr(native, "import_module", initialize)
    before = {
        entry: entry.read_bytes()
        for entry in databases.paths[0].parent.iterdir()
        if not entry.name.endswith("-shm")
    }
    with observation.observe_prepared_state(*databases.args) as unchanged:
        assert len(databases.opened) == len(set(databases.opened)) == 4
        for database, path in zip(databases.opened, databases.paths, strict=True):
            issued = factory._registered_prepared_connection(database)
            assert issued.path == path and issued.native_identity is None
            assert database.execute("SELECT * FROM records").fetchall() == [("WAL",)]
            assert database.execute("PRAGMA query_only").fetchone() == (1,)
            for query_only in ("ON", "OFF"):
                database.execute(f"PRAGMA query_only={query_only}")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    database.execute("INSERT INTO records VALUES ('forbidden')")
            assert database.total_changes == 0
        unchanged()
    assert_closed(databases)
    initialize.assert_not_called()
    assert native._state == "not_started"
    assert {entry: entry.read_bytes() for entry in before} == before
    with changed():
        unchanged()


async def test_original_task_issues_four_sources_and_child_only_observes(databases, fake_native):
    with observation.observe_prepared_state(*databases.args) as unchanged:
        assert len(fake_native.tokens) == 4
        for token, path in zip(fake_native.tokens, databases.paths, strict=True):
            issued = factory._registered_prepared_connection(token.database)
            assert issued.task is asyncio.current_task()
            assert issued.native_identity is token
            assert token.pin == (path.stat().st_dev, path.stat().st_ino)
            token.check.reset_mock()

        async def child():
            unchanged()
            for database, path in zip(databases.opened, databases.paths, strict=True):
                for operation in (
                    factory.require_prepared_git_connection,
                    factory._prepared_git_connection_observer,
                ):
                    with pytest.raises(KernelError) as caught:
                        operation(database, path)
                    assert caught.value.code == "git_prepared_link_host_invalid"
                with pytest.raises(KernelError) as caught:
                    with git_prefix_sql_window(database, checkpoint=unchanged):
                        pytest.fail("child observer cannot open a SQL window")
                assert caught.value.code == "git_prepared_link_host_invalid"

        await asyncio.create_task(child())
        for token in fake_native.tokens:
            token.check.assert_called_once()
        unchanged()
    assert_closed(databases, fake_native.tokens)


def test_native_observation_does_not_enter_pure_factory_controls(databases, fake_native):
    with observation.observe_prepared_state(*databases.args):
        for token in fake_native.tokens:
            local = factory._prepared_git_connection_lifecycle_observer(token.database)
            registration = factory._prepared_git_connection_registration_observer(token.database)
            token.check.side_effect = AssertionError("pure controls must not perform native I/O")
            local()
            registration()
            token.check.side_effect = None


@pytest.mark.parametrize("index", range(4))
def test_full_observation_rechecks_each_actual_monitor(databases, fake_native, index):
    with changed(), observation.observe_prepared_state(*databases.args) as unchanged:
        for token in fake_native.tokens:
            token.check.reset_mock()
        fake_native.tokens[index].check.return_value = False
        unchanged()
    assert [token.check.call_count for token in fake_native.tokens] == [
        int(position <= index) for position in range(4)
    ]
    assert_closed(databases, fake_native.tokens)


@pytest.mark.parametrize("index", range(4))
def test_each_monitor_compares_its_own_data_version(databases, monkeypatch, index):
    readonly = factory.readonly_database

    def prime_distinct_versions(path):
        database = readonly(path)
        writer = databases.writers[databases.paths.index(path)]
        database.execute("PRAGMA data_version").fetchone()
        for _ in range(databases.paths.index(path)):
            writer.execute("INSERT INTO records VALUES ('prime')")
            database.execute("PRAGMA data_version").fetchone()
        return database

    monkeypatch.setattr(factory, "readonly_database", prime_distinct_versions)
    with changed(), observation.observe_prepared_state(*databases.args) as unchanged:
        versions = tuple(observation._version(db) for db in databases.opened)
        assert len(set(versions)) == 4
        with closing(sqlite3.connect(databases.paths[index], isolation_level=None)) as other:
            other.execute("INSERT INTO records VALUES ('external')")
        unchanged()
    assert_closed(databases)


@pytest.mark.parametrize("index", [1, 2, 3])
def test_writer_write_then_rollback_is_not_hidden_by_unchanged_versions(databases, index):
    with changed(), observation.observe_prepared_state(*databases.args) as unchanged:
        versions = tuple(observation._version(db) for db in databases.opened)
        writer = databases.writers[index]
        writer.execute("BEGIN")
        writer.execute("INSERT INTO records VALUES ('rollback')")
        writer.rollback()
        assert tuple(observation._version(db) for db in databases.opened) == versions
        unchanged()
    assert_closed(databases)


def test_full_check_keeps_reference_identity_source_version_changes_order(
    databases, fake_native, monkeypatch
):
    events = []
    identity, version = observation._identity, observation._version
    with observation.observe_prepared_state(*databases.args, check_on_exit=False) as unchanged:
        monkeypatch.setattr(
            observation, "_identity", lambda path: (events.append("identity"), identity(path))[1]
        )
        monkeypatch.setattr(
            observation, "_version", lambda db: (events.append("version"), version(db))[1]
        )
        for token in fake_native.tokens:
            token.check.side_effect = lambda: (events.append("source"), True)[1]
        unchanged()
        assert events == ["identity"] * 4 + ["source"] * 4 + ["version"] * 4
        events.clear()
        router = databases.args[0]
        router._audit._db = databases.writers[0]
        with changed():
            unchanged()
        assert not events
        router._audit._db = databases.writers[1]
        monkeypatch.setattr(observation, "_identity", lambda path: (-1, -1))
        with changed():
            unchanged()
        assert not events


def failure(kind):
    if kind == "host":
        return KernelError("git_prepared_link_host_invalid", "source")
    if kind == "foreign":
        return KernelError("foreign_callback", "original")
    return {
        "cancel": asyncio.CancelledError,
        "timeout": TimeoutError,
        "external": RuntimeError,
    }[kind]("original")


@pytest.mark.parametrize(
    ("stage", "check_on_exit"),
    [
        (stage, check_on_exit)
        for stage in ("enter", "issue", "full", "exit", "body")
        for check_on_exit in (False, True)
        if stage != "exit" or check_on_exit
    ],
)
@pytest.mark.parametrize("kind", ["host", "foreign", "cancel", "timeout", "external"])
def test_only_source_host_errors_are_mapped_and_cleanup_preserves_first_failure(
    databases, fake_native, monkeypatch, stage, check_on_exit, kind
):
    original = failure(kind)
    attach = fake_native.attach_identity.side_effect

    def attach_with_cleanup_failure(*args):
        if stage == "enter" and len(fake_native.tokens) == 2:
            raise original
        token = attach(*args)
        token.release.side_effect = RuntimeError("secondary cleanup error")
        return token

    fake_native.attach_identity.side_effect = attach_with_cleanup_failure
    observer = observation._prepared_git_connection_observer
    issued = 0

    def issue(database, path):
        nonlocal issued
        issued += 1
        if stage == "issue" and issued == 3:
            raise original
        return observer(database, path)

    monkeypatch.setattr(observation, "_prepared_git_connection_observer", issue)
    with pytest.raises(type(original)) as caught:
        with observation.observe_prepared_state(
            *databases.args, check_on_exit=check_on_exit
        ) as unchanged:
            if stage == "body":
                raise original
            if stage in ("full", "exit"):
                fake_native.tokens[2].check.side_effect = original
                if stage == "full":
                    unchanged()
            else:
                pytest.fail("entry failure must precede yield")
    if kind == "host" and stage != "body":
        assert caught.value.code == "git_prepared_link_changed"
        assert caught.value.__suppress_context__
    else:
        assert caught.value is original
    assert_closed(databases, fake_native.tokens)


@pytest.mark.parametrize("check_on_exit", [False, True])
@pytest.mark.parametrize("kind", ["host", "foreign", "cancel", "timeout", "external"])
def test_first_cleanup_error_is_not_remapped_or_replaced(
    databases, fake_native, check_on_exit, kind
):
    original = failure(kind)
    with pytest.raises(type(original)) as caught:
        with observation.observe_prepared_state(*databases.args, check_on_exit=check_on_exit):
            for token in fake_native.tokens:
                token.release.side_effect = RuntimeError("later cleanup failure")
            fake_native.tokens[-1].release.side_effect = original
    assert caught.value is original
    assert_closed(databases, fake_native.tokens)


@pytest.mark.parametrize("index", range(4))
def test_initial_monitor_identity_failure_rejects_before_yield(databases, fake_native, index):
    attach = fake_native.attach_identity.side_effect

    def attach_invalid(*args):
        token = attach(*args)
        if len(fake_native.tokens) == index + 1:
            token.check.return_value = False
        return token

    fake_native.attach_identity.side_effect = attach_invalid
    with changed(), observation.observe_prepared_state(*databases.args):
        pytest.fail("invalid monitor must not reach caller")
    assert len(databases.opened) == index + 1
    assert_closed(databases, fake_native.tokens)


@pytest.mark.parametrize("check_on_exit", [False, True])
@pytest.mark.parametrize("exceptional", [False, True])
def test_closure_revoked_before_lifo_release_even_on_error(
    databases, fake_native, check_on_exit, exceptional
):
    releases = []
    primary = TimeoutError("body")

    def released(index):
        def release():
            calls = [token.check.call_count for token in fake_native.tokens]
            with changed():
                unchanged()
            assert calls == [token.check.call_count for token in fake_native.tokens]
            assert databases.opened[index] not in factory._owned.connections
            assert databases.opened[index].execute("SELECT 1").fetchone() == (1,)
            releases.append(index)

        return release

    with ExitStack() as errors:
        if exceptional:
            caught = errors.enter_context(pytest.raises(TimeoutError))
        with observation.observe_prepared_state(
            *databases.args, check_on_exit=check_on_exit
        ) as unchanged:
            for index, token in enumerate(fake_native.tokens):
                token.release.side_effect = released(index)
            calls = [token.check.call_count for token in fake_native.tokens]
            if exceptional:
                raise primary
    assert releases == [3, 2, 1, 0]
    assert [token.check.call_count for token in fake_native.tokens] == [
        count + int(check_on_exit and not exceptional) for count in calls
    ]
    if exceptional:
        assert caught.value is primary
    assert_closed(databases, fake_native.tokens)
    with changed():
        unchanged()
