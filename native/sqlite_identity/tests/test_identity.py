"""安装态 wheel 回归；每个场景均使用独立的隔离解释器执行。

使用已安装 wheel 的解释器：python -I -B -m pytest /absolute/path/to/tests。
未观测的历史 ABA 属于已知边界，复现并保存证据后报告为 XFAIL，不计为修复通过。
"""

import gc
import importlib
import importlib.machinery
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import pytest

SCENARIOS = {}
LIFETIME_FUNCTION = "harnessix_b7_lifetime"
QUERY = (
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100) "
    "SELECT sum(x) FROM n"
)


def scenario(function):
    SCENARIOS[function.__name__] = function
    return function


def variants(*names):
    def register(function):
        for name in names:
            SCENARIOS[f"{function.__name__}[{name}]"] = partial(function, variant=name)
        return function

    return register


def pin(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def seed(folder, name="main.db", value="A"):
    path = folder / name
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("CREATE TABLE payload(value TEXT)")
            connection.execute("INSERT INTO payload VALUES (?)", (value,))
    finally:
        connection.close()
    return path


def bootstrap():
    connection = sqlite3.connect(":memory:")
    try:
        connection.enable_load_extension(True)
        assert bridge.initialize_backend(connection) is None
        connection.enable_load_extension(False)
    finally:
        connection.close()


@contextmanager
def attached(path, *, readonly=False, **options):
    expected = pin(path)
    uri = path.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    connection = sqlite3.connect(uri, uri=True, **options)
    token = None
    try:
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        connection.enable_load_extension(True)
        token = bridge.attach_identity(connection, *expected)
        connection.enable_load_extension(False)
        yield connection, token
    finally:
        try:
            if token is not None:
                token.release()
        finally:
            connection.close()


def resources():
    gc.collect()
    counts = bridge._resource_counts()
    assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 0, counts
    assert counts["states_created"] == counts["states_freed"], counts
    return counts


@scenario
def formal_api_surface(folder):
    package = importlib.import_module("harnessix_sqlite_identity")
    public = {
        "BridgeError",
        "BackendUnavailable",
        "ConnectionIdentityError",
        "IdentityToken",
        "initialize_backend",
        "attach_identity",
    }
    assert set(package.__all__) == public
    assert {name for name in dir(bridge) if not name.startswith("_")} == public
    for name in public:
        assert getattr(package, name) is getattr(bridge, name)
    for name in (
        "attach",
        "Token",
        "stats",
        "_native_status",
        "_api_guard_probe",
        "LIBRARY_PATH",
        "DUMMY_NAME",
    ):
        assert not hasattr(bridge, name), name
        assert not hasattr(package, name), name
    assert bridge.BackendUnavailable.__bases__ == (bridge.BridgeError,)
    assert bridge.ConnectionIdentityError.__bases__ == (bridge.BridgeError,)
    assert issubclass(bridge.BridgeError, RuntimeError)
    assert not hasattr(package, "_resource_counts")
    assert all(type(value) is int and value == 0 for value in resources().values())


@scenario
def uninitialized_rejected(folder):
    path = seed(folder)
    expected = pin(path)
    connection = sqlite3.connect(path)
    try:
        connection.enable_load_extension(True)
        with pytest.raises(bridge.BackendUnavailable):
            bridge.attach_identity(connection, *expected)
        assert resources()["states_created"] == 0
        with pytest.raises(sqlite3.OperationalError):
            connection.load_extension(bridge.__file__)
        assert resources()["states_created"] == 0
        assert connection.execute("SELECT value FROM payload").fetchone() == ("A",)
    finally:
        connection.close()


@scenario
def bootstrap_engine_gate(folder):
    connection = sqlite3.connect(":memory:")
    try:
        connection.enable_load_extension(True)
        if sqlite3.sqlite_version_info == (3, 45, 3):
            assert bridge.initialize_backend(connection) is None
            connection.enable_load_extension(False)
            assert bridge.initialize_backend(connection) is None
            return {"engine_gate": "qualified"}
        with pytest.raises(bridge.BackendUnavailable, match="UNSUPPORTED"):
            bridge.initialize_backend(connection)
        with pytest.raises(bridge.BackendUnavailable):
            bridge.initialize_backend(connection)
        with pytest.raises(bridge.BackendUnavailable):
            bridge.attach_identity(connection, 0, 0)
        assert resources()["states_created"] == 0
        return {"engine_gate": "unsupported_rejected", "full_suite_is_not_skipped": True}
    finally:
        connection.close()


@variants("rw", "ro")
def original_rw_and_ro(folder, *, variant):
    bootstrap()
    path = seed(folder)
    before_bytes = path.read_bytes()
    with attached(path, readonly=variant == "ro") as (connection, token):
        connection.execute("BEGIN")
        assert connection.execute("SELECT value FROM payload").fetchone() == ("A",)
        before = connection.in_transaction, connection.total_changes
        trace = []
        connection.set_trace_callback(trace.append)
        for _ in range(30):
            assert token.check() is True
        assert not trace
        assert before == (connection.in_transaction, connection.total_changes)
        token.release()
        assert not trace and connection.in_transaction
        connection.set_trace_callback(None)
        assert connection.execute("SELECT value FROM payload").fetchone() == ("A",)
        with pytest.raises(bridge.ConnectionIdentityError):
            token.check()
        connection.rollback()
    assert path.read_bytes() == before_bytes


@variants("device", "inode")
def wrong_physical_pin(folder, *, variant):
    bootstrap()
    path = seed(folder)
    expected = pin(path)
    wrong = list(expected)
    wrong[0 if variant == "device" else 1] += 1
    connection = sqlite3.connect(path)
    try:
        connection.enable_load_extension(True)
        with pytest.raises(bridge.ConnectionIdentityError, match="IDENTITY"):
            bridge.attach_identity(connection, *wrong)
        resources()
        token = bridge.attach_identity(connection, *expected)
        try:
            assert token.check() is True
        finally:
            token.release()
    finally:
        connection.close()


@scenario
def open_b_restore_a(folder):
    bootstrap()
    main, other = seed(folder), seed(folder, "other.db", "B")
    saved = folder / "saved.db"
    expected = pin(main)
    main.rename(saved)
    other.rename(main)
    connection = sqlite3.connect(main)
    main.rename(other)
    saved.rename(main)
    try:
        assert pin(main) == expected
        connection.enable_load_extension(True)
        with pytest.raises(bridge.ConnectionIdentityError):
            bridge.attach_identity(connection, *expected)
        assert connection.execute("SELECT value FROM payload").fetchone() == ("B",)
    finally:
        connection.close()


@scenario
def moved_after_attach_stays_revoked(folder):
    bootstrap()
    main, other = seed(folder), seed(folder, "other.db", "B")
    saved = folder / "saved.db"
    with attached(main) as (_, token):
        main.rename(saved)
        other.rename(main)
        try:
            with pytest.raises(bridge.ConnectionIdentityError, match="MOVED"):
                token.check()
        finally:
            main.rename(other)
            saved.rename(main)
        with pytest.raises(bridge.ConnectionIdentityError, match="REVOKED"):
            token.check()


@variants("memory", "shared_memory", "unix-none")
def unknown_vfs_and_memory(folder, *, variant):
    bootstrap()
    path = seed(folder)
    uri = {
        "memory": ":memory:",
        "shared_memory": "file:identity-memory?mode=memory&cache=shared",
        "unix-none": path.as_uri() + "?mode=ro&vfs=unix-none",
    }[variant]
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.enable_load_extension(True)
        with pytest.raises(bridge.ConnectionIdentityError):
            bridge.attach_identity(connection, *pin(path))
        resources()
        assert connection.execute("SELECT 42").fetchone() == (42,)
    finally:
        connection.close()


@scenario
def original_progress_exception(folder):
    bootstrap()
    with attached(seed(folder), readonly=True) as (connection, token):
        original = RuntimeError("owned cancellation")
        observed = []
        calls = 0

        def progress():
            nonlocal calls
            calls += 1
            try:
                assert token.check() is True
                if calls == 7:
                    raise original
            except BaseException as error:
                observed.append(error)
                return 1
            return 0

        connection.execute("BEGIN")
        trace = []
        connection.set_trace_callback(trace.append)
        connection.set_progress_handler(progress, 1)
        try:
            with pytest.raises(sqlite3.OperationalError, match="interrupted"):
                connection.execute(QUERY).fetchall()
        finally:
            connection.set_progress_handler(None, 0)
        assert calls == 7 and len(observed) == 1 and observed[0] is original
        assert trace == [QUERY] and connection.in_transaction and connection.total_changes == 0
        assert token.check() is True


@scenario
def progress_moved_denial_propagates_without_sql(folder):
    bootstrap()
    main, other = seed(folder), seed(folder, "other.db", "B")
    saved = folder / "saved.db"
    with attached(main) as (connection, token):
        failures, trace = [], []
        previous_hook = sys.unraisablehook
        sqlite3.enable_callback_tracebacks(True)
        sys.unraisablehook = lambda event: failures.append(event.exc_value)
        connection.set_trace_callback(trace.append)
        main.rename(saved)
        other.rename(main)
        try:
            connection.set_progress_handler(lambda: int(not token.check()), 1)
            with pytest.raises(sqlite3.OperationalError, match="interrupted"):
                connection.execute(QUERY).fetchall()
            assert len(failures) == 1
            assert type(failures[0]) is bridge.ConnectionIdentityError
            assert "MOVED" in str(failures[0])
            assert len(trace) <= 1 and all(sql == QUERY for sql in trace)
        finally:
            connection.set_progress_handler(None, 0)
            sys.unraisablehook = previous_hook
            sqlite3.enable_callback_tracebacks(False)
            main.rename(other)
            saved.rename(main)
        with pytest.raises(bridge.ConnectionIdentityError, match="REVOKED"):
            token.check()


@variants("reinit", "close_reinit", "failed_reinit")
def reinitialization_and_failed_init(folder, *, variant):
    bootstrap()
    path = seed(folder)
    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    token = bridge.attach_identity(connection, *pin(path))
    try:
        if variant == "close_reinit":
            connection.close()
        if variant == "failed_reinit":
            with pytest.raises(sqlite3.OperationalError):
                connection.__init__(folder / "absent" / "never.db")
        else:
            connection.__init__(path)
        with pytest.raises(bridge.ConnectionIdentityError):
            token.check()
        assert token.release() is None
    finally:
        token.release()
        try:
            connection.close()
        except sqlite3.ProgrammingError as error:
            assert variant == "failed_reinit" and "not called" in str(error)


@scenario
def audit_reentry_other_connection(folder):
    bootstrap()
    main, other = seed(folder), seed(folder, "other.db", "B")
    first, second = sqlite3.connect(main), sqlite3.connect(other)
    expected, other_expected = pin(main), pin(other)
    first.enable_load_extension(True)
    second.enable_load_extension(True)
    inner = []
    enabled = True

    def hook(event, args):
        if enabled and event == "sqlite3.load_extension" and args[0] is first:
            with pytest.raises(bridge.ConnectionIdentityError) as caught:
                bridge.attach_identity(second, *other_expected)
            inner.append(caught.value)

    sys.addaudithook(hook)
    token = None
    try:
        token = bridge.attach_identity(first, *expected)
        enabled = False
        assert len(inner) == 1 and token.check() is True
        second.close()
        assert token.check() is True
    finally:
        enabled = False
        if token is not None:
            token.release()
        first.close()
        second.close()


@scenario
def concurrent_independent_connections(folder):
    bootstrap()
    paths = [seed(folder, f"{index}.db") for index in range(3)]
    errors, completed = [], []
    gate = threading.Barrier(3)

    def work(path):
        try:
            with attached(path) as (connection, token):
                gate.wait(timeout=5)
                for _ in range(200):
                    assert token.check() is True
                    assert connection.execute("SELECT value FROM payload").fetchone() == ("A",)
                completed.append(path.name)
        except BaseException:
            errors.append(traceback.format_exc())

    workers = [threading.Thread(target=work, args=(path,)) for path in paths]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(7)
    assert not any(worker.is_alive() for worker in workers), "workers did not finish"
    assert not errors, errors
    assert sorted(completed) == sorted(path.name for path in paths)


@scenario
def revoking_one_connection_does_not_revoke_another(folder):
    bootstrap()
    main, second = seed(folder), seed(folder, "second.db", "B")
    replacement, saved = seed(folder, "replacement.db", "X"), folder / "saved.db"
    with attached(main) as (first_connection, first_token), attached(second) as (_, second_token):
        assert first_token.check() is True and second_token.check() is True
        main.rename(saved)
        replacement.rename(main)
        try:
            with pytest.raises(bridge.ConnectionIdentityError, match="MOVED"):
                first_token.check()
            assert second_token.check() is True
            first_connection.close()
            first_token.release()
            assert second_token.check() is True
        finally:
            main.rename(replacement)
            saved.rename(main)


@variants("runtime", "sqlite", "bridge", "base_exception")
def bootstrap_audit_veto_preserves_exception(folder, *, variant):
    audit_veto(folder, operation="bootstrap", kind=variant)


@variants("runtime", "sqlite", "bridge", "base_exception")
def attach_audit_veto_preserves_exception(folder, *, variant):
    bootstrap()
    audit_veto(folder, operation="attach", kind=variant)


def audit_veto(folder, *, operation, kind):
    path = seed(folder)
    expected = pin(path)
    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    exception_type = {
        "runtime": RuntimeError,
        "sqlite": sqlite3.OperationalError,
        "bridge": bridge.ConnectionIdentityError,
        "base_exception": BaseException,
    }[kind]
    original = exception_type("UNSUPPORTED: IDENTITY: external audit veto")
    enabled, events = True, []

    def hook(event, args):
        if enabled and event == "sqlite3.load_extension" and args[0] is connection:
            events.append(args)
            raise original

    sys.addaudithook(hook)
    try:
        with pytest.raises(exception_type) as caught:
            if operation == "bootstrap":
                bridge.initialize_backend(connection)
            else:
                bridge.attach_identity(connection, *expected)
        assert caught.value is original
        assert len(events) == 1 and Path(events[0][1]).resolve() == Path(bridge.__file__).resolve()
        resources()
        enabled = False
        if operation == "bootstrap":
            with pytest.raises(bridge.BackendUnavailable):
                bridge.initialize_backend(connection)
            with pytest.raises(bridge.BackendUnavailable):
                bridge.attach_identity(connection, *expected)
        else:
            token = bridge.attach_identity(connection, *expected)
            try:
                assert token.check() is True
            finally:
                token.release()
    finally:
        enabled = False
        connection.close()


@variants(
    *(
        f"{operation}-{kind}"
        for operation in ("bootstrap", "attach")
        for kind in ("runtime", "sqlite", "bridge", "base_exception", "no_veto")
    )
)
def reentrant_entry_failure_preserves_caller_exception(folder, *, variant):
    operation, kind = variant.split("-", 1)
    if operation == "attach":
        bootstrap()
    path = seed(folder)
    connection = sqlite3.connect(path)
    other = sqlite3.connect(":memory:")
    connection.enable_load_extension(True)
    other.enable_load_extension(True)
    original = {
        "runtime": RuntimeError,
        "sqlite": sqlite3.OperationalError,
        "bridge": bridge.ConnectionIdentityError,
        "base_exception": BaseException,
        "no_veto": RuntimeError,
    }[kind]("original audit policy failure")
    active = True

    def hook(event, args):
        nonlocal active
        if active and event == "sqlite3.load_extension" and args[0] is connection:
            active = False
            # 原生入口可应答，但不能以此签发身份令牌或安装全局 callback。
            other.load_extension(bridge.__file__)
            if operation == "bootstrap":
                other.load_extension(bridge.__file__)
            if kind != "no_veto":
                raise original

    sys.addaudithook(hook)
    try:
        expected_error = (
            (
                bridge.BackendUnavailable
                if operation == "bootstrap"
                else bridge.ConnectionIdentityError
            )
            if kind == "no_veto"
            else type(original)
        )
        with pytest.raises(expected_error) as caught:
            if operation == "bootstrap":
                bridge.initialize_backend(connection)
            else:
                bridge.attach_identity(connection, *pin(path))
        if kind != "no_veto":
            assert caught.value is original
        resources()
        if operation == "bootstrap":
            with pytest.raises(bridge.BackendUnavailable):
                bridge.attach_identity(connection, *pin(path))
        else:
            token = bridge.attach_identity(connection, *pin(path))
            try:
                assert token.check() is True
            finally:
                token.release()
    finally:
        active = False
        connection.close()
        other.close()


@scenario
def gc_release_and_dummy_override(folder):
    bootstrap()
    path = seed(folder)
    before = len(os.listdir("/dev/fd"))
    for _ in range(100):
        connection = sqlite3.connect(path)
        try:
            connection.enable_load_extension(True)
            token = bridge.attach_identity(connection, *pin(path))
            assert token.check() is True
            counts = bridge._resource_counts()
            connection.create_function(LIFETIME_FUNCTION, 0, lambda: "replacement")
            assert bridge._resource_counts()["destroys"] == counts["destroys"] + 1
            with pytest.raises(bridge.ConnectionIdentityError, match="REVOKED"):
                token.check()
            with pytest.raises(bridge.ConnectionIdentityError, match="duplicate"):
                bridge.attach_identity(connection, *pin(path))
            assert bridge._resource_counts()["registrations"] == counts["registrations"]
            assert connection.execute(f"SELECT {LIFETIME_FUNCTION}()").fetchone() == (
                "replacement",
            )
            del token
        finally:
            connection.close()
        resources()
    assert len(os.listdir("/dev/fd")) == before


@scenario
def token_cannot_be_constructed_or_modified(folder):
    bootstrap()
    with attached(seed(folder)) as (_, token):
        assert type(token) is bridge.IdentityToken
        for construct in (
            bridge.IdentityToken,
            lambda: object.__new__(bridge.IdentityToken),
            lambda: bridge.IdentityToken.__new__(bridge.IdentityToken),
            lambda: type("Forged", (bridge.IdentityToken,), {}),
        ):
            with pytest.raises(TypeError):
                construct()
        for name, value in (("state", 1), ("check", lambda: True), ("release", lambda: None)):
            with pytest.raises(AttributeError):
                setattr(token, name, value)
            with pytest.raises(TypeError):
                setattr(bridge.IdentityToken, name, value)
        with pytest.raises(AttributeError):
            del token.check
        assert not hasattr(token, "__dict__")
        with pytest.raises(TypeError):
            token.check(1)
        with pytest.raises(TypeError):
            token.release(1)
        assert token.check() is True


@scenario
def exact_connection_type_required(folder):
    class ConnectionSubclass(sqlite3.Connection):
        pass

    path = seed(folder)
    subclass = sqlite3.connect(path, factory=ConnectionSubclass)
    try:
        for value in (None, object(), subclass):
            with pytest.raises(TypeError):
                bridge.initialize_backend(value)
        bootstrap()
        for value in (None, object(), subclass):
            with pytest.raises(TypeError):
                bridge.attach_identity(value, *pin(path))
        assert resources()["states_created"] == 0
    finally:
        subclass.close()


@scenario
def formal_call_signatures_reject_keywords_and_wrong_arity(folder):
    bootstrap()
    path = seed(folder)
    expected = pin(path)
    connection = sqlite3.connect(path)
    try:
        before = resources()
        for operation in (
            lambda: bridge.initialize_backend(),
            lambda: bridge.initialize_backend(connection, connection),
            lambda: bridge.initialize_backend(connection=connection),
            lambda: bridge.attach_identity(connection),
            lambda: bridge.attach_identity(connection, *expected, 0),
            lambda: bridge.attach_identity(
                connection, expected_dev=expected[0], expected_ino=expected[1]
            ),
        ):
            with pytest.raises(TypeError):
                operation()
        assert resources() == before
    finally:
        connection.close()


PIN_KINDS = (
    "none",
    "true",
    "false",
    "float",
    "text",
    "bytes",
    "object",
    "int_subclass",
    "indexable",
    "negative",
    "negative_large",
    "overflow",
    "overflow_large",
)


@variants(*(f"{field}-{kind}" for field in ("device", "inode") for kind in PIN_KINDS))
def invalid_pin_rejected_before_native_allocation(folder, *, variant):
    class IntegerSubclass(int):
        pass

    class Indexable:
        def __index__(self):
            raise AssertionError("pin coercion must not call user code")

    bootstrap()
    path = seed(folder)
    field, kind = variant.split("-", 1)
    invalid = {
        "none": None,
        "true": True,
        "false": False,
        "float": 1.0,
        "text": "1",
        "bytes": b"1",
        "object": object(),
        "int_subclass": IntegerSubclass(1),
        "indexable": Indexable(),
        "negative": -1,
        "negative_large": -(1 << 128),
        "overflow": 1 << 64,
        "overflow_large": 1 << 128,
    }[kind]
    expected = list(pin(path))
    expected[0 if field == "device" else 1] = invalid
    error_type = OverflowError if kind.startswith(("negative", "overflow")) else TypeError
    connection = sqlite3.connect(path)
    try:
        connection.enable_load_extension(True)
        before = resources()
        with pytest.raises(error_type):
            bridge.attach_identity(connection, *expected)
        assert resources() == before
    finally:
        connection.close()


@variants("device-zero", "inode-zero", "device-uint64_max", "inode-uint64_max")
def integer_pin_bounds_do_not_wrap(folder, *, variant):
    bootstrap()
    path = seed(folder)
    expected = list(pin(path))
    field, bound = variant.split("-", 1)
    value = 0 if bound == "zero" else (1 << 64) - 1
    index = 0 if field == "device" else 1
    assert expected[index] != value, "fixture needs a distinct physical pin"
    expected[index] = value
    connection = sqlite3.connect(path)
    try:
        connection.enable_load_extension(True)
        with pytest.raises(bridge.ConnectionIdentityError, match="IDENTITY"):
            bridge.attach_identity(connection, *expected)
    finally:
        connection.close()


@scenario
def original_thread_owns_token(folder):
    bootstrap()
    with attached(seed(folder), check_same_thread=False) as (_, token):
        errors, outcomes = [], []

        def worker():
            try:
                for operation in (token.check, token.release):
                    with pytest.raises(bridge.ConnectionIdentityError, match="THREAD"):
                        operation()
                    outcomes.append(operation.__name__)
            except BaseException:
                errors.append(traceback.format_exc())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        assert outcomes == ["check", "release"]
        assert token.check() is True


@scenario
def cross_thread_attach_preserves_sqlite_error(folder):
    bootstrap()
    path = seed(folder)
    expected = pin(path)
    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    outcomes = []

    def worker():
        try:
            bridge.attach_identity(connection, *expected)
        except BaseException as error:
            outcomes.append(error)

    try:
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert len(outcomes) == 1 and type(outcomes[0]) is sqlite3.ProgrammingError
        resources()
        token = bridge.attach_identity(connection, *expected)
        try:
            assert token.check() is True
        finally:
            token.release()
    finally:
        connection.close()


@scenario
def duplicate_attach_cannot_replace_original_token(folder):
    bootstrap()
    path = seed(folder)
    with attached(path) as (connection, token):
        connection.enable_load_extension(True)
        before = bridge._resource_counts()
        with pytest.raises(bridge.ConnectionIdentityError, match="duplicate"):
            bridge.attach_identity(connection, *pin(path))
        assert token.check() is True
        after = bridge._resource_counts()
        assert after["registrations"] == before["registrations"]
        assert after["states_live"] == after["leases_live"] == after["userdata_live"] == 1
        token.release()
        with pytest.raises(bridge.ConnectionIdentityError, match="duplicate"):
            bridge.attach_identity(connection, *pin(path))


@scenario
def close_defers_native_destroy_until_release(folder):
    bootstrap()
    with attached(seed(folder)) as (connection, token):
        connection.close()
        counts = bridge._resource_counts()
        assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 1
        with pytest.raises(bridge.ConnectionIdentityError, match="CLOSED"):
            token.check()
        assert bridge._resource_counts()["file_controls"] == counts["file_controls"]
        assert token.release() is None
        assert token.release() is None
        resources()


@scenario
def release_is_idempotent_and_rejects_future_checks(folder):
    bootstrap()
    with attached(seed(folder)) as (connection, token):
        before = connection.in_transaction, connection.total_changes
        assert token.release() is None
        assert token.release() is None
        counts = bridge._resource_counts()
        with pytest.raises(bridge.ConnectionIdentityError, match="REVOKED_OR_RELEASED"):
            token.check()
        assert bridge._resource_counts()["file_controls"] == counts["file_controls"]
        assert before == (connection.in_transaction, connection.total_changes)
        assert counts["leases_live"] == 0 and counts["userdata_live"] == 1


@scenario
def attach_prepares_one_lease_without_sql_or_new_connections(folder):
    bootstrap()
    path = seed(folder)
    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    expected = pin(path)
    events, trace, authorizations = [], [], []
    enabled = True

    def hook(event, args):
        if enabled and event.startswith("sqlite3."):
            events.append((event, args))

    sys.addaudithook(hook)
    connection.set_trace_callback(trace.append)
    connection.set_authorizer(lambda *args: authorizations.append(args) or sqlite3.SQLITE_OK)
    token = None
    try:
        before = connection.in_transaction, connection.total_changes
        token = bridge.attach_identity(connection, *expected)
        assert len(authorizations) == 1 and authorizations[0][0] == sqlite3.SQLITE_SELECT
        for _ in range(30):
            assert token.check() is True
        token.release()
        assert not trace and len(authorizations) == 1
        assert before == (connection.in_transaction, connection.total_changes)
        assert len(events) == 1 and events[0][0] == "sqlite3.load_extension"
        assert events[0][1][0] is connection
        return {"executed_sql": trace, "lease_prepares": 1, "new_connections": 0}
    finally:
        enabled = False
        if token is not None:
            token.release()
        connection.close()


@variants("legacy", "autocommit_false", "autocommit_true", "savepoint", "readonly")
def transactions_and_callbacks_survive_attach_check_release(folder, *, variant):
    bootstrap()
    path = seed(folder)
    options = {"autocommit": variant == "autocommit_true"} if "autocommit" in variant else {}
    connection = sqlite3.connect(
        path.as_uri() + ("?mode=ro" if variant == "readonly" else "?mode=rw"),
        uri=True,
        **options,
    )
    token = None
    try:
        if variant == "savepoint":
            connection.execute("SAVEPOINT retained")
        if variant in ("legacy", "savepoint", "autocommit_false"):
            connection.execute("UPDATE payload SET value='pending'")
        before = connection.in_transaction, connection.total_changes
        trace, progress = [], []
        connection.set_trace_callback(trace.append)
        connection.set_progress_handler(lambda: progress.append(1) or 0, 1)
        connection.enable_load_extension(True)
        token = bridge.attach_identity(connection, *pin(path))
        after_prepare = len(progress)
        for _ in range(30):
            assert token.check() is True
        token.release()
        assert not trace and len(progress) == after_prepare
        assert before == (connection.in_transaction, connection.total_changes)
        assert connection.execute("SELECT 42").fetchone() == (42,)
        assert trace == ["SELECT 42"] and len(progress) > after_prepare
        connection.set_progress_handler(None, 0)
        if variant == "savepoint":
            connection.execute("ROLLBACK TO retained")
            connection.execute("RELEASE retained")
        elif variant in ("legacy", "autocommit_false"):
            connection.rollback()
        assert connection.execute("SELECT value FROM payload").fetchone() == ("A",)
    finally:
        if token is not None:
            token.release()
        connection.close()


@variants("bootstrap", "attach")
def disabled_extension_loading_is_not_enabled_implicitly(folder, *, variant):
    if variant == "attach":
        bootstrap()
    path = seed(folder)
    connection = sqlite3.connect(path)
    connection.enable_load_extension(False)
    try:
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            if variant == "bootstrap":
                bridge.initialize_backend(connection)
            else:
                bridge.attach_identity(connection, *pin(path))
        resources()
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            connection.load_extension(bridge.__file__)
        if variant == "bootstrap":
            connection.enable_load_extension(True)
            with pytest.raises(bridge.BackendUnavailable):
                bridge.initialize_backend(connection)
    finally:
        connection.close()


@variants("denied_lease", "busy_dummy", "closed_connection")
def native_failure_classification_and_cleanup(folder, *, variant):
    bootstrap()
    path = seed(folder)
    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    cursor = None
    try:
        if variant == "denied_lease":
            connection.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
        elif variant == "busy_dummy":
            connection.create_function(LIFETIME_FUNCTION, 0, lambda: "existing")
            cursor = connection.execute(
                f"SELECT {LIFETIME_FUNCTION}() UNION ALL SELECT {LIFETIME_FUNCTION}()"
            )
        else:
            connection.close()
        with pytest.raises(bridge.ConnectionIdentityError):
            bridge.attach_identity(connection, *pin(path))
        resources()
        if cursor is not None:
            assert cursor.fetchall() == [("existing",), ("existing",)]
            cursor.close()
        if variant != "closed_connection":
            connection.set_authorizer(None)
            token = bridge.attach_identity(connection, *pin(path))
            try:
                assert token.check() is True
            finally:
                token.release()
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


@scenario
def dummy_is_directonly_and_returns_no_identity(folder):
    bootstrap()
    with attached(seed(folder)) as (connection, token):
        rows = [
            row for row in connection.execute("PRAGMA function_list") if row[0] == LIFETIME_FUNCTION
        ]
        assert len(rows) == 1
        flags = rows[0][5]
        assert flags & 0x80000 and not flags & 0x800
        assert connection.execute(f"SELECT {LIFETIME_FUNCTION}()").fetchone() == (None,)
        connection.execute(f"CREATE VIEW forbidden AS SELECT {LIFETIME_FUNCTION}()")
        with pytest.raises(sqlite3.OperationalError, match="unsafe use"):
            connection.execute("SELECT * FROM forbidden").fetchall()
        assert token.check() is True


@scenario
def unrelated_sql_errors_do_not_revoke_identity(folder):
    bootstrap()
    with attached(seed(folder)) as (connection, token):
        connection.execute("CREATE TABLE unique_values(value INTEGER UNIQUE)")
        connection.execute("INSERT INTO unique_values VALUES (1)")
        for statement, error_type in (
            ("SELECT absent_column", sqlite3.OperationalError),
            ("INSERT INTO unique_values VALUES (1)", sqlite3.IntegrityError),
        ):
            with pytest.raises(error_type):
                connection.execute(statement)
            trace = []
            connection.set_trace_callback(trace.append)
            assert token.check() is True
            assert not trace
            connection.set_trace_callback(None)


@variants("check", "release")
def cross_thread_sql_callback_does_not_deadlock_owner(folder, *, variant):
    bootstrap()
    with attached(seed(folder), check_same_thread=False) as (connection, token):
        entered, gate = threading.Event(), threading.Event()
        result = {}

        def progress():
            if not entered.is_set():
                entered.set()
                assert gate.wait(3), "callback gate timeout"
            return 0

        def execute():
            try:
                result["sum"] = connection.execute(QUERY).fetchone()[0]
            except BaseException:
                result["error"] = traceback.format_exc()

        connection.set_progress_handler(progress, 1)
        worker = threading.Thread(target=execute)
        unblocker = threading.Timer(0.05, gate.set)
        worker.start()
        try:
            assert entered.wait(3), "worker never reached callback"
            unblocker.start()
            observed = getattr(token, variant)()
            assert observed is (True if variant == "check" else None)
            worker.join(3)
            assert not worker.is_alive() and result == {"sum": 5050}, result
        finally:
            gate.set()
            worker.join(3)
            unblocker.cancel()
            if unblocker.ident is not None:
                unblocker.join(3)
            connection.set_progress_handler(None, 0)


@scenario
def release_gc_and_callback_cycle_reclaim_resources(folder):
    bootstrap()
    path = seed(folder)
    expected = pin(path)
    before = len(os.listdir("/dev/fd"))
    for index in range(120):
        connection = sqlite3.connect(path)
        connection.enable_load_extension(True)
        token = bridge.attach_identity(connection, *expected)
        assert token.check() is True
        if index % 3 == 0:
            connection.close()
            token.release()
        elif index % 3 == 1:
            token.release()
            connection.close()
        else:
            del token
            gc.collect()
            connection.close()
        resources()

    def make_cycle():
        connection = sqlite3.connect(path)
        connection.enable_load_extension(True)
        token = bridge.attach_identity(connection, *expected)
        connection.set_progress_handler(lambda: int(not token.check()), 100)

    make_cycle()
    resources()
    assert len(os.listdir("/dev/fd")) == before


@scenario
def known_boundary_unobserved_historical_aba(folder):
    bootstrap()
    main, other = seed(folder), seed(folder, "other.db", "B")
    saved, expected = folder / "saved.db", pin(main)
    with attached(main) as (connection, token):
        main.rename(saved)
        other.rename(main)
        main.rename(other)
        saved.rename(main)
        assert pin(main) == expected
        assert token.check() is True
        assert connection.execute("SELECT value FROM payload").fetchone() == ("A",)
    return {
        "known_boundary": True,
        "unobserved_transient_replacement_detected": False,
        "scope": "point-in-time identity, not historical continuity; no B handle was opened",
    }


def installed_bridge():
    module = importlib.import_module("harnessix_sqlite_identity._bridge")
    path = Path(module.__file__).resolve()
    distribution = importlib.metadata.distribution("harnessix-sqlite-identity")
    assert distribution.read_text("WHEEL"), "wheel installation required"
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    assert not direct_url.get("dir_info", {}).get("editable"), "editable install is not regression"
    assert any(str(path).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
    assert not path.is_relative_to(Path(__file__).resolve().parents[1] / "src")
    assert any(
        Path(distribution.locate_file(file)).resolve() == path for file in distribution.files or ()
    ), "extension missing from installed RECORD"
    return module, {"path": str(path), "distribution_version": distribution.version}


def run_child(name, folder):
    global bridge
    started = time.monotonic()
    record = {
        "case": name,
        "status": "failed",
        "pid": os.getpid(),
        "python": sys.version,
        "executable": sys.executable,
        "sqlite_version": sqlite3.sqlite_version,
        "isolated": sys.flags.isolated,
        "dont_write_bytecode": sys.dont_write_bytecode,
    }
    try:
        assert sys.flags.isolated and sys.dont_write_bytecode and not sys.flags.optimize
        bridge, record["installed_bridge"] = installed_bridge()
        record["evidence"] = SCENARIOS[name](folder)
        record["resources"] = resources()
        record["status"] = (
            "known_boundary_confirmed"
            if name == "known_boundary_unobserved_historical_aba"
            else "passed"
        )
    except BaseException:
        record["traceback"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 4)
        (folder / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return int(record["status"] == "failed")


@pytest.mark.parametrize("name", SCENARIOS)
def test_installed_scenario(name, tmp_path):
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), name, str(tmp_path)]
    process = {"command": command, "timeout_seconds": 30}
    with (tmp_path / "run.log").open("w", encoding="utf-8") as output:
        try:
            result = subprocess.run(
                command,
                cwd=tmp_path,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            process["returncode"] = result.returncode
        except subprocess.TimeoutExpired:
            process["timed_out"] = True
    (tmp_path / "process.json").write_text(json.dumps(process, indent=2) + "\n", encoding="utf-8")
    detail = (tmp_path / "run.log").read_text(encoding="utf-8", errors="replace")
    assert not process.get("timed_out"), f"scenario timed out; evidence: {tmp_path}\n{detail}"
    assert process["returncode"] == 0, f"scenario failed; evidence: {tmp_path}\n{detail}"
    record = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert record["case"] == name
    if name == "known_boundary_unobserved_historical_aba":
        assert record["status"] == "known_boundary_confirmed"
        pytest.xfail(
            f"historical ABA remains unobservable, not a repair PASS; evidence: {tmp_path}"
        )
    assert record["status"] == "passed", record


if __name__ == "__main__":
    raise SystemExit(run_child(sys.argv[1], Path(sys.argv[2])))
