"""两份非 editable 安装件的消费回归；不修改产品默认装配。"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

QUERY = (
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) "
    "SELECT sum(x) FROM n"
)


def seed(folder, name, value="A"):
    path = folder / name
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE payload(value)")
        db.execute("INSERT INTO payload VALUES (?)", (value,))
        db.commit()
    finally:
        db.close()
    return path


@contextmanager
def candidate_connection(path, readonly):
    """测试专用装配：原 factory 准入后消费其原 issued pin，不新增路径见证。"""
    with factory.open_prepared_git_connection(path, read_only=readonly) as db:
        issued = factory._registered_prepared_connection(db)
        assert issued is not None and issued[1] == issued[2]
        token = None
        try:
            db.enable_load_extension(True)
            try:
                token = bridge.attach_identity(db, *issued[1][-1])
            finally:
                db.enable_load_extension(False)
            yield db, token
        finally:
            if token is not None:
                token.release()


def rejected(operation, expected):
    try:
        operation()
    except BaseException as error:
        assert isinstance(error, expected), type(error)
        return error
    raise AssertionError("unexpected acceptance")


def require_candidate(db, token, path):
    factory.require_prepared_git_connection(db, path)
    assert token.check()


def source_and_transaction(folder):
    for readonly in (False, True):
        path = seed(folder, f"{readonly}.db")
        before = path.read_bytes()
        with candidate_connection(path, readonly) as (db, token):
            require_candidate(db, token, path)
            with sql._git_prefix_transaction_scope(db):
                db.execute("BEGIN")
                original = sql._git_prefix_caller_transaction_epoch(db)
                with sql.git_prefix_sql_window(db, checkpoint=token.check):
                    epoch = sql.git_prefix_transaction_epoch(db)
                    assert db.execute("SELECT value FROM payload").fetchone() == ("A",)
                    assert token.check()
                    sql.require_git_prefix_transaction_epoch(db, epoch)
                    sql._require_git_prefix_caller_transaction_epoch(db, original)
                if readonly:
                    rejected(
                        lambda: db.execute("INSERT INTO payload VALUES ('blocked')"),
                        sqlite3.OperationalError,
                    )
                db.rollback()
                rejected(
                    lambda epoch=original: sql._require_git_prefix_caller_transaction_epoch(
                        db, epoch
                    ),
                    KernelError,
                )
        assert path.read_bytes() == before
        rejected(lambda: db.execute("SELECT 1"), sqlite3.ProgrammingError)
    return {"factory_modes": 2, "original_epoch_preserved": True, "new_writes": 0}


def original_control_failures(folder):
    facts = []
    for index, original in enumerate(
        (
            asyncio.CancelledError(),
            TimeoutError("deadline"),
            KernelError("git_prepared_link_host_invalid", "source"),
            RuntimeError("owner callback"),
        )
    ):
        for with_identity in (False, True):
            path = seed(folder, f"{index}-{with_identity}.db")
            facts.append(run_control_failure(path, original, with_identity))
    return {"paired_controls": facts, "original_progress_interval": 1000}


def run_control_failure(path, original, with_identity):
    before = path.read_bytes()
    calls = 0
    with candidate_connection(path, True) as (db, token):
        registration = factory._prepared_git_connection_registration_observer(db)

        def checkpoint():
            nonlocal calls
            calls += 1
            registration()
            if with_identity:
                assert token.check()
            if calls == 7:
                raise original

        def execute():
            with sql.git_prefix_sql_window(db, checkpoint=checkpoint):
                db.execute(QUERY).fetchall()

        error = rejected(execute, type(original))
        assert error is original and calls == 7
        assert db.execute("SELECT value FROM payload").fetchone() == ("A",)
        assert token.check() and db.total_changes == 0
    assert before == path.read_bytes()
    return {
        "failure_type": type(original).__name__,
        "identity_enabled": with_identity,
        "same_exception": True,
        "checkpoint_count": calls,
    }


def task_and_pure_observer(folder):
    path = seed(folder, "a.db")

    async def run():
        with candidate_connection(path, True) as (db, token):
            pure = factory._prepared_git_connection_lifecycle_observer(db)
            registration = factory._prepared_git_connection_registration_observer(db)
            trace = []
            db.set_trace_callback(trace.append)

            def forbidden(*args, **kwargs):
                raise AssertionError("pure observer added path or SQL I/O")

            with (
                patch.object(factory, "_physical_pin", forbidden),
                patch.object(factory, "_database_path", forbidden),
            ):
                pure()
                registration()
                assert not trace
            db.set_trace_callback(None)

            async def sibling():
                error = rejected(lambda: require_candidate(db, token, path), KernelError)
                assert error.code == "git_prepared_link_host_invalid"
                registration()  # 已签发的观察不授予兄弟Task使用原连接的能力。

            await asyncio.create_task(sibling())
            require_candidate(db, token, path)
        error = rejected(registration, KernelError)
        assert error.code == "git_prepared_link_host_invalid"
        rejected(token.check, bridge.BridgeError)

    asyncio.run(run())
    return {"original_task_boundary": True, "pure_observers_unchanged": True}


def original_factory_aba(folder):
    a, b = seed(folder, "a.db"), seed(folder, "b.db", "B")
    saved = folder / "saved.db"
    connect = sqlite3.connect
    actual_connection = None
    legacy_accepted = False

    def swapped(*args, **kwargs):
        nonlocal actual_connection
        a.rename(saved)
        b.rename(a)
        try:
            actual_connection = connect(*args, **kwargs)
            return actual_connection
        finally:
            a.rename(b)
            saved.rename(a)

    # 旧 factory 的 before/after pin保持A；保留旧准入观察，不把旧PASS抹成拒绝。
    with patch.object(sqlite3, "connect", swapped):
        with factory.open_prepared_git_connection(a, read_only=True) as db:
            factory.require_prepared_git_connection(db, a)
            legacy_accepted = True
            assert db.execute("SELECT value FROM payload").fetchone() == ("B",)
    with patch.object(sqlite3, "connect", swapped):

        def new_admission():
            with candidate_connection(a, True):
                raise AssertionError("wrong original file admitted")

        rejected(new_admission, bridge.ConnectionIdentityError)
    rejected(lambda: actual_connection.execute("SELECT 1"), sqlite3.ProgrammingError)
    return {
        "original_path_factory_accepts": legacy_accepted,
        "native_candidate_rejects": True,
        "expected_pin_origin": "original_factory_registration",
        "failure_closes_original": True,
    }


def readonly_wal_snapshot(folder):
    path = seed(folder, "wal.db")
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        with candidate_connection(path, True) as (reader, token):
            reader.execute("BEGIN")
            assert reader.execute("SELECT value FROM payload").fetchone() == ("A",)
            writer.execute("UPDATE payload SET value='B'")
            writer.commit()
            assert token.check()
            assert reader.execute("SELECT value FROM payload").fetchone() == ("A",)
            reader.rollback()
            assert reader.execute("SELECT value FROM payload").fetchone() == ("B",)
            assert reader.total_changes == 0
    finally:
        writer.close()
    return {
        "real_WAL_snapshot_preserved": True,
        "identity_is_not_owner_freshness": True,
        "wal_shm_identity_verified": False,
    }


def native_failure_through_original_sql_window(folder):
    path = seed(folder, "a.db")
    replacement = seed(folder, "b.db", "B")
    retired = folder / "retired.db"
    with candidate_connection(path, True) as (db, token):
        calls = 0
        observed = []

        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 7:
                path.rename(retired)
                replacement.rename(path)
            try:
                token.check()
            except bridge.BridgeError as error:
                observed.append(error)
                raise

        def execute():
            with sql.git_prefix_sql_window(db, checkpoint=checkpoint):
                db.execute(QUERY).fetchall()

        try:
            propagated = rejected(execute, bridge.BridgeError)
            assert len(observed) == 1 and propagated is observed[0]
        finally:
            if retired.exists():
                path.rename(replacement)
                retired.rename(path)
        assert calls == 7 and db.execute("SELECT value FROM payload").fetchone() == ("A",)
        rejected(token.check, bridge.BridgeError)
    return {
        "original_native_exception_not_swallowed": True,
        "public_error_mapping": "not_implemented",
    }


CASES = {
    function.__name__: function
    for function in (
        source_and_transaction,
        original_control_failures,
        task_and_pure_observer,
        original_factory_aba,
        readonly_wal_snapshot,
        native_failure_through_original_sql_window,
    )
}


@pytest.mark.parametrize("scenario", CASES)
def test_installed_product_consumer(scenario, tmp_path):
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), scenario, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=45,
        cwd=tmp_path,
    )
    (tmp_path / "run.log").write_text(completed.stdout + completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "PASS"


if __name__ == "__main__":
    import traceback

    import harnessix_sqlite_identity as bridge
    from harnessix_sqlite_identity import _bridge

    # 在任何产品模块或 SQLite worker 启动前初始化；仅独占测试子进程具备此前提。
    bootstrap = sqlite3.connect(":memory:")
    try:
        bootstrap.enable_load_extension(True)
        bridge.initialize_backend(bootstrap)
        bootstrap.enable_load_extension(False)
    finally:
        bootstrap.close()

    from harnessix.agent.errors import KernelError
    from harnessix.product_config import git_prefix_sql as sql
    from harnessix.product_config import git_prepared_link_connection as factory

    folder = Path(sys.argv[2])
    try:
        assert "site-packages" in Path(bridge.__file__).parts
        assert "site-packages" in Path(factory.__file__).parts
        observed = CASES[sys.argv[1]](folder)
        counts = _bridge._resource_counts()
        assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 0
        result = {"status": "PASS", "observed": observed, "resources": counts}
    except BaseException:
        result = {"status": "FAIL", "traceback": traceback.format_exc()}
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    if result["status"] != "PASS":
        print(result["traceback"])
        raise SystemExit(1)
