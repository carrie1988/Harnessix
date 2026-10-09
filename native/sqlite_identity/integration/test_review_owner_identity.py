"""安装件 Owner 鲜读连接的实际 FD 身份；每个场景在独占进程初始化。"""

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest


def resources_released():
    counts = _bridge._resource_counts()
    assert counts["states_live"] == counts["leases_live"] == counts["userdata_live"] == 0


@contextmanager
def audit_case(folder, *, wal=True):
    folder.mkdir(parents=True, exist_ok=True)
    with SQLiteActionAuditStore(folder / "原 Audit #%.db", require_runtime_owner=True) as audit:
        if not wal:
            assert audit._db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        with bind_product_audit_identity(audit), audit.runtime_owner():
            yield audit
    resources_released()


def observe(audit):
    hosts._read_fresh_owner(
        audit,
        audit._path,
        hosts._audit_file_identity(audit._path),
        original=audit._db,
    )


def assert_closed(database):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


@contextmanager
def host_invalid():
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == "git_action_review_host_invalid"


def fresh_views_are_readonly_and_borrowed_audit_survives(folder):
    for wal in (False, True):
        with audit_case(folder / str(wal), wal=wal) as audit:
            original, changes = audit._db, audit._db.total_changes
            read_owner, views = audit._read_runtime_owner, []

            def inspect(*, database, original=original, read_owner=read_owner, views=views):
                assert database is not original
                assert _bridge._resource_counts()["leases_live"] == 2
                assert database.execute("PRAGMA query_only").fetchone() == (1,)
                database.execute("PRAGMA query_only=OFF")
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    database.execute("UPDATE action_audit_metadata SET value='forbidden'")
                database.execute("PRAGMA query_only=ON")
                assert database.total_changes == 0
                views.append(database)
                return read_owner(database=database)

            with patch.object(audit, "_read_runtime_owner", inspect):
                for _ in range(3):
                    observe(audit)
                    assert _bridge._resource_counts()["leases_live"] == 1
            assert len(views) == 3 and audit._db is original
            assert original.total_changes == changes and not original.in_transaction
            assert original.execute("SELECT 1").fetchone() == (1,)
            for view in views:
                assert_closed(view)


def fresh_opened_b_then_path_restored_a(folder):
    with audit_case(folder, wal=False) as audit:
        path, replacement, saved = audit._path, folder / "other.db", folder / "saved.db"
        with closing(sqlite3.connect(replacement)) as other:
            audit._db.backup(other)
        pin, opened, readonly = hosts._audit_file_identity(path), [], hosts.readonly_database

        def swap(candidate):
            path.rename(saved)
            replacement.rename(path)
            try:
                view = readonly(candidate)
                opened.append(view)
                return view
            finally:
                path.rename(replacement)
                saved.rename(path)

        with (
            patch.object(hosts, "readonly_database", swap),
            patch.object(audit, "_read_runtime_owner", side_effect=AssertionError("no Owner read")),
            host_invalid(),
        ):
            observe(audit)
        assert hosts._audit_file_identity(path) == pin
        assert len(opened) == 1
        assert_closed(opened[0])
        assert audit._db.execute("SELECT 1").fetchone() == (1,)
        assert _bridge._resource_counts()["leases_live"] == 1


def closed_fresh_handle_after_owner_read_is_rejected(folder):
    with audit_case(folder) as audit:
        read_owner, views = audit._read_runtime_owner, []

        def close_after_read(*, database):
            result = read_owner(database=database)
            views.append(database)
            database.close()
            return result

        with patch.object(audit, "_read_runtime_owner", close_after_read), host_invalid():
            observe(audit)
        assert_closed(views[0])
        assert _bridge._resource_counts()["leases_live"] == 1
        assert audit._db.execute("SELECT 1").fetchone() == (1,)


def owner_first_failure_and_revoked_observer(folder):
    errors = (
        asyncio.CancelledError(),
        TimeoutError("deadline"),
        KernelError("git_prepared_link_host_invalid", "Owner failure is not native failure"),
        KernelError("action_runtime_fence_lost", "Owner changed"),
    )
    for index, error in enumerate(errors):
        with audit_case(folder / str(index)) as audit:
            with patch.object(audit, "_read_runtime_owner", side_effect=error):
                with pytest.raises(type(error)) as caught:
                    observe(audit)
            assert caught.value is error
            assert _bridge._resource_counts()["leases_live"] == 1
            with closing(hosts.readonly_database(audit._path)) as view:
                with hosts._observe_review_connection(
                    view, hosts._audit_file_identity(audit._path)
                ) as check:
                    check()
            with host_invalid():
                check()
            assert audit._db.execute("SELECT 1").fetchone() == (1,)


def stale_wal_owner_is_not_replaced_by_identity(folder):
    with audit_case(folder) as audit:
        cursor = audit._db.execute("SELECT key,value FROM action_audit_metadata ORDER BY key")
        assert cursor.fetchone() is not None
        try:
            with closing(sqlite3.connect(audit._path)) as other, other:
                other.execute(
                    "UPDATE action_audit_metadata SET value='changed' WHERE key='owner_generation'"
                )
            assert not audit._db.in_transaction
            assert audit._read_runtime_owner() is audit._runtime_fence
            with pytest.raises(KernelError) as caught:
                observe(audit)
            assert caught.value.code == "action_runtime_fence_lost"
        finally:
            cursor.close()


CASES = (
    fresh_views_are_readonly_and_borrowed_audit_survives,
    fresh_opened_b_then_path_restored_a,
    closed_fresh_handle_after_owner_read_is_rejected,
    owner_first_failure_and_revoked_observer,
    stale_wal_owner_is_not_replaced_by_identity,
)


@pytest.mark.parametrize("case", [case.__name__ for case in CASES])
def test_installed_review_owner(tmp_path, case):
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
    from harnessix.product_config import git_prepared_native_identity as identity

    assert sys.version_info[:2] == (3, 12)
    assert sqlite3.sqlite_version == "3.45.3"
    identity.initialize_prepared_git_identity()
    from harnessix_sqlite_identity import _bridge

    from harnessix.product_config import git_delivery_review_host as hosts
    from harnessix.product_config.git_review_identity import bind_product_audit_identity
    from harnessix.trusted_actions.store import SQLiteActionAuditStore

    for module in (identity, hosts):
        assert "site-packages" in Path(module.__file__).parts
    folder = Path(sys.argv[2])
    next(case for case in CASES if case.__name__ == sys.argv[1])(folder)
    resources_released()
    (folder / "result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
                "consumer": hosts.__file__,
                "consumer_sha256": hashlib.sha256(Path(hosts.__file__).read_bytes()).hexdigest(),
                "resources": _bridge._resource_counts(),
            },
            indent=2,
        )
    )
