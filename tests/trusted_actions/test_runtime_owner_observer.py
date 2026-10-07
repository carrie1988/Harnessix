"""同一原Owner算法借只读观察连接；不替换原业务连接或改变写准入。"""

import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sqlite_readonly import readonly_database
from harnessix.trusted_actions.store import SQLiteActionAuditStore


@pytest.mark.parametrize("field", ["owner_generation", "owner_token_sha256"])
@pytest.mark.parametrize("change", ["replace", "delete"])
def test_fresh_observer_rejects_owner_hidden_by_original_implicit_snapshot(tmp_path, field, change):
    path = tmp_path / "audit.db"
    with SQLiteActionAuditStore(path, require_runtime_owner=True) as audit:
        with audit.runtime_owner() as fence:
            original = audit._db
            cursor = original.execute("SELECT key,value FROM action_audit_metadata ORDER BY key")
            assert cursor.fetchone() is not None
            try:
                with closing(sqlite3.connect(path)) as external, external:
                    if change == "delete":
                        external.execute("DELETE FROM action_audit_metadata WHERE key=?", (field,))
                    else:
                        external.execute(
                            "UPDATE action_audit_metadata SET value='changed' WHERE key=?", (field,)
                        )
                assert not original.in_transaction
                assert audit._read_runtime_owner() is fence
                with closing(readonly_database(path)) as observer:
                    changes = observer.total_changes
                    with pytest.raises(KernelError) as caught:
                        audit._read_runtime_owner(database=observer)
                    assert caught.value.code == "action_runtime_fence_lost"
                    assert observer.total_changes == changes == 0
                assert audit._db is original
            finally:
                cursor.close()


def test_observer_uses_same_fence_and_keeps_original_write_transaction(tmp_path):
    path = tmp_path / "audit.db"
    with SQLiteActionAuditStore(path, require_runtime_owner=True) as audit:
        with audit.runtime_owner() as fence:
            original = audit._db
            original.execute("BEGIN IMMEDIATE")
            changes = original.total_changes
            try:
                with closing(readonly_database(path)) as observer:
                    statements = []
                    observer.set_trace_callback(statements.append)
                    assert audit._read_runtime_owner(database=observer) is fence
                    assert observer.total_changes == 0 and not observer.in_transaction
                    assert len(statements) == 1 and statements[0].startswith("SELECT ")
                    assert fence.token not in statements[0]
                assert audit._db is original and original.in_transaction
                assert audit._assert_runtime_owner() is fence
                assert original.total_changes == changes
            finally:
                original.execute("ROLLBACK")


def test_readonly_observer_cannot_update_owner_or_create_business_rows(tmp_path):
    path = tmp_path / "audit.db"
    with SQLiteActionAuditStore(path, require_runtime_owner=True) as audit:
        with audit.runtime_owner() as fence:
            with closing(readonly_database(path)) as observer:
                assert observer.execute("PRAGMA query_only").fetchone() == (1,)
                with pytest.raises(sqlite3.OperationalError):
                    observer.execute("UPDATE action_audit_metadata SET value='changed'")
                with pytest.raises(sqlite3.OperationalError):
                    observer.execute("CREATE TABLE other(value TEXT)")
                assert audit._read_runtime_owner(database=observer) is fence
                assert observer.total_changes == 0
