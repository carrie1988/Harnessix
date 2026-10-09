"""实际安装件的原 Audit 生命周期接线；独占启动，使用真实 Store、FD 和宿主。"""

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest


class NoSecrets:
    def resolve(self, _reference):
        raise AssertionError("生命周期验证不得读取凭据")


def released():
    counts = _bridge._resource_counts()
    assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 0


@contextmanager
def audit_store(folder, *, wal=True):
    folder.mkdir(parents=True, exist_ok=True)
    with SQLiteActionAuditStore(folder / "action-audit.db", require_runtime_owner=True) as audit:
        if not wal:
            assert audit._db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        yield audit
    released()


def observer(audit):
    return identity.original_audit_observer(
        audit, audit._db, audit._path, identity._audit_file_identity(audit._path)
    )


def fresh_owner(audit):
    hosts._read_fresh_owner(
        audit, audit._path, identity._audit_file_identity(audit._path), original=audit._db
    )


@contextmanager
def invalid():
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == "git_action_review_host_invalid"


def assert_closed(database):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


def actual_product_host_reuses_one_original_token(folder):
    async def run():
        attached = []
        attach = native.attach_prepared_identity

        def record(backend, database, pin):
            attached.append(database)
            return attach(backend, database, pin)

        with patch.object(native, "attach_prepared_identity", record):
            async with runtime._open_action_dependencies(folder / "state", NoSecrets()) as deps:
                audit, original = deps.audit, deps.audit._db
                assert audit._runtime_fence is deps.fence
                assert _bridge._resource_counts()["leases_live"] == 1
                initial_changes = original.total_changes
                for _ in range(5):
                    check = observer(audit)
                    check()
                    fresh_owner(audit)
                    assert _bridge._resource_counts()["leases_live"] == 1
                assert attached.count(original) == 1
                assert len(attached) == len(set(attached)) == 6
                assert original.total_changes == initial_changes and not original.in_transaction
                assert audit._read_runtime_owner() is deps.fence
            assert_closed(original)
            with invalid():
                check()
        released()

    asyncio.run(run())


def missing_or_expired_scope_never_attaches_on_demand(folder):
    with audit_store(folder) as audit, audit.runtime_owner():
        before = _bridge._resource_counts()["registrations"]
        with patch.object(hosts, "readonly_database", side_effect=AssertionError("no fresh open")):
            with invalid():
                fresh_owner(audit)
        assert _bridge._resource_counts()["registrations"] == before
        with identity.bind_product_audit_identity(audit):
            check = observer(audit)
            check()
            with invalid():
                with identity.bind_product_audit_identity(audit):
                    pytest.fail("重复宿主不得占用原令牌")
            check()
        assert _bridge._resource_counts()["leases_live"] == 0
        assert audit._db.execute("SELECT 1").fetchone() == (1,)
        with invalid():
            check()
        with invalid():
            observer(audit)
        with invalid():
            with identity.bind_product_audit_identity(audit):
                pytest.fail("release 不解除原生重复绑定禁令")


def wrong_actual_handle_is_rejected_without_closing_it(folder):
    with audit_store(folder, wal=False) as audit:
        original = audit._db
        with closing(sqlite3.connect(folder / "other.db")) as other:
            original.backup(other)
            audit._db = other
            try:
                with invalid():
                    with identity.bind_product_audit_identity(audit):
                        pytest.fail("同正文的另一文件不能成为原 Audit")
                assert other.execute("SELECT 1").fetchone() == (1,)
                assert not getattr(identity._audit_connections, "registrations", {})
            finally:
                audit._db = original
            assert original.execute("SELECT 1").fetchone() == (1,)


def observed_move_cannot_be_repaired_by_restoring_path(folder):
    with audit_store(folder, wal=False) as audit:
        with identity.bind_product_audit_identity(audit):
            check = observer(audit)
            saved = folder / "saved.db"
            audit._path.rename(saved)
            try:
                with invalid():
                    check()
            finally:
                saved.rename(audit._path)
            with invalid():
                check()
            with invalid():
                fresh_owner(audit)


def same_python_connection_reinitialization_is_rejected(folder):
    with audit_store(folder, wal=False) as audit:
        with closing(sqlite3.connect(folder / "other.db")) as other:
            audit._db.backup(other)
        with identity.bind_product_audit_identity(audit):
            check = observer(audit)
            database = audit._db
            database.__init__(folder / "other.db")
            assert audit._db is database
            with invalid():
                check()
            with invalid():
                fresh_owner(audit)


def child_task_can_observe_but_foreign_thread_cannot(folder):
    async def run():
        async with runtime._open_action_dependencies(folder / "state", NoSecrets()) as deps:
            check = observer(deps.audit)

            async def child():
                check()
                observer(deps.audit)()

            await asyncio.create_task(child())
            counts = _bridge._resource_counts()["file_controls"]
            outcomes = []

            def foreign():
                for operation in (check, lambda: observer(deps.audit)):
                    try:
                        operation()
                    except KernelError as error:
                        outcomes.append(error.code)

            thread = threading.Thread(target=foreign)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert outcomes == ["git_action_review_host_invalid"] * 2
            assert _bridge._resource_counts()["file_controls"] == counts
            check()
        released()

    asyncio.run(run())


def class_drift_is_rejected_without_foreign_callbacks(folder):
    with audit_store(folder) as audit:
        touched = []

        class ForeignAudit(SQLiteActionAuditStore):
            def __hash__(self):
                touched.append("hash")
                raise AssertionError("foreign hash")

            def __getattribute__(self, name):
                touched.append(name)
                raise AssertionError("foreign field")

        database = audit._db
        try:
            with identity.bind_product_audit_identity(audit):
                check = observer(audit)
                object.__setattr__(audit, "__class__", ForeignAudit)
                with invalid():
                    check()
        finally:
            object.__setattr__(audit, "__class__", SQLiteActionAuditStore)
        assert not touched
        assert not getattr(identity._audit_connections, "registrations", {})
        assert _bridge._resource_counts()["leases_live"] == 0
        assert database.execute("SELECT 1").fetchone() == (1,)


def startup_and_body_failures_release_before_store_close(folder):
    async def run():
        for index, error in enumerate((asyncio.CancelledError(), TimeoutError("deadline"))):
            for startup in (False, True):
                opened = []
                store = runtime.SQLiteActionAuditStore

                def record(*args, store=store, opened=opened, **kwargs):
                    audit = store(*args, **kwargs)
                    opened.append(audit)
                    return audit

                with patch.object(runtime, "SQLiteActionAuditStore", record):
                    if startup:
                        with patch.object(runtime, "WorkspaceLeaseStore", side_effect=error):
                            with pytest.raises(type(error)) as caught:
                                async with runtime._open_action_dependencies(
                                    folder / f"{index}-{startup}", NoSecrets()
                                ):
                                    pytest.fail("启动失败不得发布宿主")
                    else:
                        with pytest.raises(type(error)) as caught:
                            async with runtime._open_action_dependencies(
                                folder / f"{index}-{startup}", NoSecrets()
                            ):
                                assert _bridge._resource_counts()["leases_live"] == 1
                                raise error
                assert caught.value is error
                assert len(opened) == 1
                assert_closed(opened[0]._db)
                assert not getattr(identity._audit_connections, "registrations", {})
                released()

    asyncio.run(run())


CASES = (
    actual_product_host_reuses_one_original_token,
    missing_or_expired_scope_never_attaches_on_demand,
    wrong_actual_handle_is_rejected_without_closing_it,
    observed_move_cannot_be_repaired_by_restoring_path,
    same_python_connection_reinitialization_is_rejected,
    child_task_can_observe_but_foreign_thread_cannot,
    startup_and_body_failures_release_before_store_close,
    class_drift_is_rejected_without_foreign_callbacks,
)


@pytest.mark.parametrize("case", [case.__name__ for case in CASES])
def test_installed_audit_lifetime(tmp_path, case):
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), case, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
    )
    (tmp_path / "run.log").write_text(completed.stdout + completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr


if __name__ == "__main__":
    from harnessix.agent.errors import KernelError
    from harnessix.product_config import git_prepared_native_identity as native

    assert sys.version_info[:2] == (3, 12)
    assert sqlite3.sqlite_version == "3.45.3"
    native.initialize_prepared_git_identity()
    from harnessix_sqlite_identity import _bridge

    from harnessix.product_config import action_runtime as runtime
    from harnessix.product_config import git_delivery_review_host as hosts
    from harnessix.product_config import git_review_identity as identity
    from harnessix.trusted_actions.store import SQLiteActionAuditStore

    for module in (identity, hosts, runtime):
        assert "site-packages" in Path(module.__file__).parts
    folder = Path(sys.argv[2])
    next(case for case in CASES if case.__name__ == sys.argv[1])(folder)
    released()
    (folder / "result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
                "sources": {
                    module.__file__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                    for module in (identity, hosts, runtime)
                },
                "resources": _bridge._resource_counts(),
            },
            indent=2,
        )
    )
