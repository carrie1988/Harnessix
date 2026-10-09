"""R4 原生接入的离线控制契约；fake bridge 不证明真实 FD 或 SQLite 后端资格。"""

from __future__ import annotations

import asyncio
import importlib
import runpy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_prepared_link_connection as connection
from harnessix.product_config import git_prepared_native_identity as native
from tests.product_config.test_git_prepared_link_connection import _closed, _database, _invalid


class _BridgeError(RuntimeError):
    pass


class _AuditError(RuntimeError):
    pass


class _ExtensionConnection:
    """只用于扩展授权窗口单测；工厂测试仍使用 exact sqlite3.Connection。"""

    def __init__(self):
        self.events = []
        self.enable_load_extension = Mock(side_effect=self._enable)
        self.close = Mock(side_effect=lambda: self.events.append("close"))

    def _enable(self, allowed):
        self.events.append(("extension", allowed))


@pytest.fixture(autouse=True)
def isolated_native_state(monkeypatch):
    # 模拟独占启动，不让本文件的状态污染同进程内旧合作式边界回归。
    monkeypatch.setattr(native, "_state", "not_started")
    monkeypatch.setattr(native, "_backend", None)
    monkeypatch.setattr(native, "_connections_started", False)
    monkeypatch.setattr(connection._owned, "connections", {}, raising=False)


@pytest.fixture
def bridge(monkeypatch):
    token = SimpleNamespace(check=Mock(return_value=True), release=Mock())
    backend = SimpleNamespace(
        BridgeError=_BridgeError,
        initialize_backend=Mock(),
        attach_identity=Mock(return_value=token),
        token=token,
    )
    monkeypatch.setattr(native, "import_module", Mock(return_value=backend))
    return backend


@pytest.fixture
def bootstrap(monkeypatch, bridge):
    database = _ExtensionConnection()
    monkeypatch.setattr(native.sqlite3, "connect", Mock(return_value=database))
    monkeypatch.setattr(native.threading, "active_count", lambda: 1)
    return database


@pytest.fixture
def ready(monkeypatch, bridge):
    monkeypatch.setattr(native, "_state", "ready")
    monkeypatch.setattr(native, "_backend", bridge)
    return bridge


def _assert_bootstrap_failed():
    assert native._state == "failed"
    assert native._backend is None
    with _invalid():
        native.initialize_prepared_git_identity()
    with _invalid():
        native.prepared_identity_backend()
    assert native._connections_started is True


def test_import_has_no_implicit_bootstrap_or_connection(monkeypatch, bridge):
    connect = Mock(side_effect=AssertionError("import must not open SQLite"))
    monkeypatch.setattr(native.sqlite3, "connect", connect)
    import_module = Mock(side_effect=AssertionError("import must not load the native bridge"))
    monkeypatch.setattr(importlib, "import_module", import_module)
    fresh = runpy.run_path(native.__file__)
    assert fresh["_state"] == "not_started"
    assert fresh["_backend"] is None
    assert fresh["_connections_started"] is False
    connect.assert_not_called()
    import_module.assert_not_called()
    bridge.initialize_backend.assert_not_called()


def test_explicit_bootstrap_publishes_backend_only_after_loading(bridge, bootstrap):
    def initialize(database):
        assert database is bootstrap
        assert native._state == "loading"
        assert native._backend is None
        assert native._connections_started is False
        database.events.append("initialize")

    bridge.initialize_backend.side_effect = initialize
    assert native.initialize_prepared_git_identity() is None
    native.import_module.assert_called_once_with("harnessix_sqlite_identity")
    native.sqlite3.connect.assert_called_once_with(":memory:")
    assert bootstrap.events == [("extension", True), "initialize", ("extension", False), "close"]
    assert native._state == "ready"
    assert native._backend is bridge
    assert native._connections_started is False
    assert native.prepared_identity_backend() is bridge
    assert native._connections_started is True


def test_successful_bootstrap_is_not_idempotent(bridge, bootstrap):
    native.initialize_prepared_git_identity()
    with _invalid():
        native.initialize_prepared_git_identity()
    bridge.initialize_backend.assert_called_once_with(bootstrap)
    native.import_module.assert_called_once()


@pytest.mark.parametrize("failure_type", [ImportError, OSError])
def test_import_failure_is_mapped_and_permanently_latched(bridge, bootstrap, failure_type):
    native.import_module.side_effect = failure_type("offline import failure")
    with _invalid():
        native.initialize_prepared_git_identity()
    _assert_bootstrap_failed()
    native.import_module.assert_called_once()
    native.sqlite3.connect.assert_not_called()


@pytest.mark.parametrize("stage", ["connect", "enable", "initialize", "disable"])
def test_sqlite_bootstrap_failure_is_mapped_and_closes_opened_connection(bridge, bootstrap, stage):
    failure = sqlite3.OperationalError("bootstrap failure")
    if stage == "connect":
        native.sqlite3.connect.side_effect = failure
    elif stage == "initialize":
        bridge.initialize_backend.side_effect = failure
    else:
        bootstrap.enable_load_extension.side_effect = (
            [failure] if stage == "enable" else [None, failure]
        )
    with _invalid():
        native.initialize_prepared_git_identity()
    _assert_bootstrap_failed()
    assert bootstrap.close.call_count == int(stage != "connect")


def test_bridge_bootstrap_failure_is_mapped_without_retry(bridge, bootstrap):
    bridge.initialize_backend.side_effect = _BridgeError("unqualified backend")
    with _invalid():
        native.initialize_prepared_git_identity()
    _assert_bootstrap_failed()
    bridge.initialize_backend.assert_called_once()
    bootstrap.close.assert_called_once()


@pytest.mark.parametrize("failure_type", [_AuditError, asyncio.CancelledError])
@pytest.mark.parametrize("stage", ["import", "enable", "initialize", "disable"])
def test_bootstrap_preserves_non_bridge_exception_and_latches_failure(
    bridge, bootstrap, failure_type, stage
):
    failure = failure_type("original audit or cancellation")
    if stage == "import":
        native.import_module.side_effect = failure
    elif stage == "initialize":
        bridge.initialize_backend.side_effect = failure
    else:
        bootstrap.enable_load_extension.side_effect = (
            [failure] if stage == "enable" else [None, failure]
        )
    with pytest.raises(failure_type) as caught:
        native.initialize_prepared_git_identity()
    assert caught.value is failure
    _assert_bootstrap_failed()
    assert bootstrap.close.call_count == int(stage != "import")


@pytest.mark.parametrize("failure_type", [_AuditError, asyncio.CancelledError])
def test_bootstrap_close_failure_does_not_replace_primary(bridge, bootstrap, failure_type):
    failure = failure_type("bootstrap primary")
    bridge.initialize_backend.side_effect = failure
    bootstrap.close.side_effect = sqlite3.OperationalError("cleanup failure")
    with pytest.raises(failure_type) as caught:
        native.initialize_prepared_git_identity()
    assert caught.value is failure
    _assert_bootstrap_failed()


@pytest.mark.parametrize("restriction", ["other_thread", "multiple_threads", "factory_used"])
def test_bootstrap_requires_exclusive_main_thread_startup(
    monkeypatch, bridge, bootstrap, restriction
):
    if restriction == "other_thread":
        monkeypatch.setattr(native.threading, "current_thread", lambda: object())
    elif restriction == "multiple_threads":
        monkeypatch.setattr(native.threading, "active_count", lambda: 2)
    else:
        assert native.prepared_identity_backend() is None
    with _invalid():
        native.initialize_prepared_git_identity()
    _assert_bootstrap_failed()
    native.import_module.assert_not_called()
    native.sqlite3.connect.assert_not_called()


async def test_running_event_loop_blocks_bootstrap_before_import(bridge, bootstrap):
    with _invalid():
        native.initialize_prepared_git_identity()
    _assert_bootstrap_failed()
    native.import_module.assert_not_called()
    native.sqlite3.connect.assert_not_called()


def test_actual_worker_thread_cannot_initialize(bridge):
    with ThreadPoolExecutor(max_workers=1) as executor:
        with _invalid():
            executor.submit(native.initialize_prepared_git_identity).result()
    _assert_bootstrap_failed()
    native.import_module.assert_not_called()


def test_live_background_thread_blocks_main_thread_bootstrap(bridge):
    started, stop = threading.Event(), threading.Event()

    def wait_for_stop():
        started.set()
        stop.wait(5)

    worker = threading.Thread(target=wait_for_stop)
    worker.start()
    try:
        assert started.wait(5)
        with _invalid():
            native.initialize_prepared_git_identity()
    finally:
        stop.set()
        worker.join(5)
    assert not worker.is_alive()
    _assert_bootstrap_failed()
    native.import_module.assert_not_called()


@pytest.mark.parametrize("state", ["loading", "failed", "ready"])
def test_missing_backend_never_falls_back_after_explicit_start(monkeypatch, bridge, state):
    monkeypatch.setattr(native, "_state", state)
    with _invalid():
        native.prepared_identity_backend()
    assert native._connections_started is True
    native.import_module.assert_not_called()


def test_none_backend_and_token_leave_legacy_connection_untouched():
    database = _ExtensionConnection()
    assert native.attach_prepared_identity(None, database, (7, 11)) is None
    assert native.check_prepared_identity(None) is None
    database.enable_load_extension.assert_not_called()
    database.close.assert_not_called()


def test_attach_uses_exact_prepared_pin_and_scoped_extension_permission(bridge):
    database = _ExtensionConnection()

    def attach(db, device, inode):
        assert db is database
        assert (device, inode) == (7, 11)
        database.events.append("attach")
        return bridge.token

    bridge.attach_identity.side_effect = attach
    assert native.attach_prepared_identity(bridge, database, (7, 11)) is bridge.token
    assert database.events == [("extension", True), "attach", ("extension", False)]
    bridge.token.check.assert_not_called()
    bridge.token.release.assert_not_called()
    database.close.assert_not_called()


@pytest.mark.parametrize("failure_type", [_BridgeError, sqlite3.OperationalError])
def test_attach_maps_bridge_and_sqlite_failure_and_disables_extension(bridge, failure_type):
    database = _ExtensionConnection()
    bridge.attach_identity.side_effect = failure_type("attach rejected")
    with _invalid():
        native.attach_prepared_identity(bridge, database, (7, 11))
    assert database.enable_load_extension.call_args_list == [call(True), call(False)]
    bridge.token.release.assert_not_called()


@pytest.mark.parametrize("failure_type", [_AuditError, asyncio.CancelledError])
@pytest.mark.parametrize("disable_failure", [False, True])
def test_attach_cleanup_preserves_primary_audit_or_cancellation(
    bridge, failure_type, disable_failure
):
    database = _ExtensionConnection()
    failure = failure_type("attach primary")
    bridge.attach_identity.side_effect = failure
    if disable_failure:
        database.enable_load_extension.side_effect = [None, sqlite3.OperationalError("disable")]
    with pytest.raises(failure_type) as caught:
        native.attach_prepared_identity(bridge, database, (7, 11))
    assert caught.value is failure
    assert database.enable_load_extension.call_args_list == [call(True), call(False)]


@pytest.mark.parametrize("release_failure", [False, True])
def test_disable_audit_failure_releases_unreturned_token_without_masking_it(
    bridge, release_failure
):
    database = _ExtensionConnection()
    failure = _AuditError("disable audit")
    database.enable_load_extension.side_effect = [None, failure]
    if release_failure:
        bridge.token.release.side_effect = _BridgeError("release failure")
    with pytest.raises(_AuditError) as caught:
        native.attach_prepared_identity(bridge, database, (7, 11))
    assert caught.value is failure
    bridge.token.release.assert_called_once()


@pytest.mark.parametrize("result", [False, None, 0, 1, "true", object()])
def test_identity_check_accepts_only_literal_true(ready, result):
    ready.token.check.return_value = result
    with _invalid():
        native.check_prepared_identity(ready.token)
    ready.token.check.assert_called_once()


def test_identity_check_true_does_not_release_or_reinitialize(ready):
    assert native.check_prepared_identity(ready.token) is None
    ready.token.check.assert_called_once()
    ready.token.release.assert_not_called()
    ready.initialize_backend.assert_not_called()


@pytest.mark.parametrize("failure_type", [_BridgeError, sqlite3.OperationalError])
def test_identity_check_maps_backend_failure(ready, failure_type):
    ready.token.check.side_effect = failure_type("native check failure")
    with _invalid():
        native.check_prepared_identity(ready.token)


@pytest.mark.parametrize("failure_type", [_AuditError, asyncio.CancelledError])
def test_identity_check_preserves_non_bridge_failure(ready, failure_type):
    failure = failure_type("native check primary")
    ready.token.check.side_effect = failure
    with pytest.raises(failure_type) as caught:
        native.check_prepared_identity(ready.token)
    assert caught.value is failure


@pytest.mark.parametrize("state", ["not_started", "loading", "failed"])
def test_identity_check_rejects_non_ready_backend_without_native_io(monkeypatch, ready, state):
    monkeypatch.setattr(native, "_state", state)
    with _invalid():
        native.check_prepared_identity(ready.token)
    ready.token.check.assert_not_called()


@pytest.mark.parametrize("read_only", [False, True])
def test_uninitialized_factory_keeps_legacy_mode_and_prevents_late_start(
    tmp_path, bridge, read_only
):
    path = _database(tmp_path / "legacy.db")
    with connection.open_prepared_git_connection(path, read_only=read_only) as database:
        issued = connection._registered_prepared_connection(database)
        assert issued.native_identity is None
        connection.require_prepared_git_connection(database, path)
        assert native._state == "not_started"
        assert native._connections_started is True
    _closed(database)
    with _invalid():
        native.initialize_prepared_git_identity()
    assert native._state == "failed"
    native.import_module.assert_not_called()
    bridge.attach_identity.assert_not_called()


@pytest.mark.parametrize("state", ["loading", "failed"])
def test_failed_or_loading_factory_never_opens_sqlite(tmp_path, monkeypatch, bridge, state):
    path = _database(tmp_path / "blocked.db")
    monkeypatch.setattr(native, "_state", state)
    connect = Mock(side_effect=AssertionError("must not fall back to a SQLite connection"))
    monkeypatch.setattr(connection.sqlite3, "connect", connect)
    with _invalid(), connection.open_prepared_git_connection(path, read_only=True):
        pytest.fail("blocked native backend yielded a legacy connection")
    connect.assert_not_called()
    bridge.attach_identity.assert_not_called()
    assert native._connections_started is True


@pytest.mark.parametrize("read_only", [False, True])
async def test_real_factory_binds_before_pin_and_preserves_source_tuple_slots(
    tmp_path, monkeypatch, ready, read_only
):
    path = _database(tmp_path / "native.db")
    before_bytes = path.read_bytes()
    physical_pin = connection._physical_pin
    pins = []

    def record_pin(*args):
        pin = physical_pin(*args)
        pins.append(pin)
        return pin

    def attach(database, device, inode):
        assert type(database) is sqlite3.Connection
        assert len(pins) == 2 and pins[0] == pins[1]
        assert (device, inode) == pins[0][-1]
        assert connection._registered_prepared_connection(database) is None
        return ready.token

    monkeypatch.setattr(connection, "_physical_pin", record_pin)
    ready.attach_identity.side_effect = attach
    with connection.open_prepared_git_connection(path, read_only=read_only) as database:
        issued = connection._registered_prepared_connection(database)
        assert isinstance(issued, tuple) and len(issued) == 5
        assert issued[:4] == (path, pins[0], pins[1], asyncio.current_task())
        assert issued.native_identity is issued[4] is ready.token
        assert ready.token.check.call_count >= 1
        connection.require_prepared_git_connection(database, path)
        assert database.execute("SELECT * FROM records").fetchall() == [("A",)]
        assert database.total_changes == 0
    ready.token.release.assert_called_once()
    assert not connection._owned.connections
    _closed(database)
    assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("read_only", [False, True])
def test_pin_mismatch_rejects_before_attaching_native_identity(
    tmp_path, monkeypatch, ready, read_only
):
    path = _database(tmp_path / "original.db")
    replacement = _database(tmp_path / "replacement.db", "B")
    connect = sqlite3.connect
    opened = []

    def swap_after_open(*args, **kwargs):
        database = connect(*args, **kwargs)
        opened.append(database)
        path.rename(tmp_path / "retired.db")
        replacement.rename(path)
        return database

    monkeypatch.setattr(connection.sqlite3, "connect", swap_after_open)
    with _invalid(), connection.open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("changed before/after pin must reject before attach")
    ready.attach_identity.assert_not_called()
    assert not connection._owned.connections
    assert len(opened) == 1
    _closed(opened[0])


@pytest.mark.parametrize("read_only", [False, True])
def test_attach_closes_connection_then_raises_audit_error_preserves_original(
    tmp_path, ready, read_only
):
    path = _database(tmp_path / "audit.db")
    failure = _AuditError("attach audit closed original connection")
    opened = []

    def close_then_raise(database, *_pin):
        opened.append(database)
        database.close()
        raise failure

    ready.attach_identity.side_effect = close_then_raise
    with pytest.raises(_AuditError) as caught:
        with connection.open_prepared_git_connection(path, read_only=read_only):
            pytest.fail("audit failure must happen before yield")
    assert caught.value is failure
    assert len(opened) == 1
    _closed(opened[0])
    assert not connection._owned.connections
    ready.token.release.assert_not_called()


@pytest.mark.parametrize("stage", ["attach", "check", "checkpoint"])
def test_close_before_yield_rejects_and_releases_token(tmp_path, ready, stage):
    path = _database(tmp_path / "closed-before-yield.db")

    def attach(database, *_pin):
        if stage == "attach":
            database.close()
        return ready.token

    def check():
        if stage == "check":
            ready.attach_identity.call_args.args[0].close()
        return True

    def checkpoint():
        if stage == "checkpoint" and ready.attach_identity.called:
            ready.attach_identity.call_args.args[0].close()

    ready.attach_identity.side_effect = attach
    ready.token.check.side_effect = check
    with (
        _invalid(),
        connection.open_prepared_git_connection(path, read_only=True, checkpoint=checkpoint),
    ):
        pytest.fail("closed connection cannot be registered or yielded")
    ready.token.release.assert_called_once()
    _closed(ready.attach_identity.call_args.args[0])
    assert not connection._owned.connections


@pytest.mark.parametrize("failure", ["attach", "check_false", "check_error"])
@pytest.mark.parametrize("read_only", [False, True])
def test_native_failure_before_yield_closes_connection_and_releases_issued_token(
    tmp_path, ready, failure, read_only
):
    path = _database(tmp_path / "failure.db")
    if failure == "attach":
        ready.attach_identity.side_effect = _BridgeError("attach failure")
    elif failure == "check_false":
        ready.token.check.return_value = False
    else:
        ready.token.check.side_effect = _BridgeError("check failure")
    with _invalid(), connection.open_prepared_git_connection(path, read_only=read_only):
        pytest.fail("native failure cannot yield a connection")
    database = ready.attach_identity.call_args.args[0]
    _closed(database)
    assert not connection._owned.connections
    assert ready.token.release.call_count == int(failure != "attach")


def test_checkpoint_revocation_after_attach_cannot_yield_invalid_identity(tmp_path, ready):
    path = _database(tmp_path / "revoked-before-yield.db")
    revoked = False

    def revoke_after_attach():
        nonlocal revoked
        if ready.attach_identity.called:
            revoked = True
            ready.token.check.return_value = False

    try:
        with connection.open_prepared_git_connection(
            path, read_only=True, checkpoint=revoke_after_attach
        ):
            # 把检查移动到最后一个 callback 之后，或不再回调，均可避免无效交接。
            assert not revoked, "last checkpoint revoked token but factory still yielded it"
    except KernelError as error:
        assert error.code == "git_prepared_link_host_invalid"
        assert revoked
    ready.token.release.assert_called_once()
    _closed(ready.attach_identity.call_args.args[0])
    assert not connection._owned.connections


@pytest.mark.parametrize("failure_type", [_AuditError, asyncio.CancelledError, TimeoutError])
@pytest.mark.parametrize("stage", ["after_attach", "body", "exit_checkpoint"])
def test_factory_cleanup_preserves_original_control_failure(tmp_path, ready, failure_type, stage):
    path = _database(tmp_path / "control.db")
    failure = failure_type("original control failure")
    ready.token.release.side_effect = _BridgeError("secondary release failure")
    body_finished = False

    def checkpoint():
        if (stage == "after_attach" and ready.attach_identity.called) or (
            stage == "exit_checkpoint" and body_finished
        ):
            raise failure

    with pytest.raises(failure_type) as caught:
        with connection.open_prepared_git_connection(path, read_only=True, checkpoint=checkpoint):
            if stage == "body":
                raise failure
            body_finished = True
    assert caught.value is failure
    ready.token.release.assert_called_once()
    _closed(ready.attach_identity.call_args.args[0])
    assert not connection._owned.connections


def test_release_precedes_real_close_and_uncommitted_transaction_is_rolled_back(tmp_path, ready):
    path = _database(tmp_path / "rollback.db")

    def release():
        assert database.in_transaction
        assert connection._registered_prepared_connection(database) is None
        assert database.execute("SELECT * FROM records").fetchall() == [("A",), ("pending",)]

    ready.token.release.side_effect = release
    with connection.open_prepared_git_connection(path, read_only=False) as database:
        database.execute("BEGIN IMMEDIATE")
        database.execute("INSERT INTO records VALUES ('pending')")
    ready.token.release.assert_called_once()
    _closed(database)
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT * FROM records").fetchall() == [("A",)]
    reader.close()


def test_release_failure_still_closes_real_connection(tmp_path, ready):
    path = _database(tmp_path / "release.db")
    failure = _BridgeError("release failure without primary")
    ready.token.release.side_effect = failure
    with pytest.raises(_BridgeError) as caught:
        with connection.open_prepared_git_connection(path, read_only=True) as database:
            pass
    assert caught.value is failure
    _closed(database)
    assert not connection._owned.connections


@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, TimeoutError])
def test_both_cleanup_failures_preserve_active_control_exception(ready, failure_type):
    database = _ExtensionConnection()
    failure = failure_type("primary control exception")
    ready.token.release.side_effect = _BridgeError("release failure")
    database.close.side_effect = sqlite3.OperationalError("close failure")
    with pytest.raises(failure_type) as caught:
        try:
            raise failure
        finally:
            connection._close_connection(database, ready.token)
    assert caught.value is failure
    ready.token.release.assert_called_once()
    database.close.assert_called_once()


@pytest.mark.parametrize("failure_type", [KernelError, asyncio.CancelledError, TimeoutError])
def test_full_source_checkpoint_failure_precedes_native_io(tmp_path, ready, failure_type):
    path = _database(tmp_path / "checkpoint.db")
    failure = (
        KernelError("original_control_failure", "原控制异常")
        if failure_type is KernelError
        else failure_type("original control exception")
    )
    with connection.open_prepared_git_connection(path, read_only=True) as database:
        ready.token.check.reset_mock()

        def checkpoint():
            raise failure

        with pytest.raises(failure_type) as caught:
            connection.require_prepared_git_connection(database, path, checkpoint=checkpoint)
        assert caught.value is failure
        ready.token.check.assert_not_called()


@pytest.mark.parametrize("read_only", [False, True])
def test_full_source_checks_token_but_pure_observers_do_no_io(
    tmp_path, monkeypatch, ready, read_only
):
    path = _database(tmp_path / "observers.db")
    with connection.open_prepared_git_connection(path, read_only=read_only) as database:
        full = connection._prepared_git_connection_observer(database, path)
        ready.token.check.reset_mock()
        statements = []
        database.set_trace_callback(statements.append)
        forbidden = Mock(side_effect=AssertionError("pure observer must not perform source IO"))
        with monkeypatch.context() as patch:
            patch.setattr(connection, "_physical_pin", forbidden)
            patch.setattr(connection, "_database_path", forbidden)
            patch.setattr(ready.token, "check", forbidden)
            lifecycle = connection._prepared_git_connection_lifecycle_observer(database)
            registration = connection._prepared_git_connection_registration_observer(database)
            connection._registered_prepared_connection(database)
            lifecycle()
            registration()
            forbidden.assert_not_called()
            assert statements == []
        database.set_trace_callback(None)
        full()
        ready.token.check.assert_called_once()
        ready.token.check.return_value = False
        lifecycle()
        registration()
        for observe in (full, lambda: connection.require_prepared_git_connection(database, path)):
            with _invalid():
                observe()
    for observe in (full, lifecycle, registration):
        with _invalid():
            observe()


@pytest.mark.parametrize("damage", ["closed", "replaced", "backend_failed"])
def test_full_observer_rejects_closed_or_invalidated_source(tmp_path, monkeypatch, ready, damage):
    path = _database(tmp_path / "source.db")
    replacement = _database(tmp_path / "replacement.db", "B")
    with connection.open_prepared_git_connection(path, read_only=True) as database:
        full = connection._prepared_git_connection_observer(database, path)
        if damage == "closed":
            database.close()
        elif damage == "replaced":
            path.rename(tmp_path / "retired.db")
            replacement.rename(path)
        else:
            monkeypatch.setattr(native, "_state", "failed")
        for observe in (full, lambda: connection.require_prepared_git_connection(database, path)):
            with _invalid():
                observe()


async def test_native_token_does_not_grant_other_task_sql_or_lifecycle_admission(tmp_path, ready):
    path = _database(tmp_path / "task.db")
    with connection.open_prepared_git_connection(path, read_only=True) as database:
        full = connection._prepared_git_connection_observer(database, path)
        lifecycle = connection._prepared_git_connection_lifecycle_observer(database)
        registration = connection._prepared_git_connection_registration_observer(database)

        async def other_task():
            for operation in (
                lifecycle,
                lambda: connection.require_prepared_git_connection(database, path),
                lambda: connection._registered_prepared_connection(database),
            ):
                with _invalid():
                    operation()
            registration()
            checks = ready.token.check.call_count
            full()
            assert ready.token.check.call_count == checks + 1

        await asyncio.create_task(other_task())
        connection.require_prepared_git_connection(database, path)
        lifecycle()


def test_native_source_and_observers_cannot_cross_threads(tmp_path, ready):
    path = _database(tmp_path / "thread.db")
    with connection.open_prepared_git_connection(path, read_only=True) as database:
        full = connection._prepared_git_connection_observer(database, path)
        lifecycle = connection._prepared_git_connection_lifecycle_observer(database)
        registration = connection._prepared_git_connection_registration_observer(database)
        ready.token.check.reset_mock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            for operation in (
                full,
                lifecycle,
                registration,
                lambda: connection.require_prepared_git_connection(database, path),
            ):
                with _invalid():
                    executor.submit(operation).result()
        ready.token.check.assert_not_called()
        connection.require_prepared_git_connection(database, path)
