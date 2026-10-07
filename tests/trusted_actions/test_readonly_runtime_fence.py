"""原Owner算法的只读入口：不取得新代次，不绕过末端禁止写入边界。"""

from __future__ import annotations

import sqlite3

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.terminal_read_control import terminal_read_scope


@pytest.mark.parametrize("require_owner", [False, True])
def test_readonly_fence_preserves_original_no_owner_behavior(tmp_path, require_owner):
    with SQLiteActionAuditStore(
        tmp_path / "audit.db", require_runtime_owner=require_owner
    ) as audit:
        changes = audit._db.total_changes
        if require_owner:
            with pytest.raises(KernelError) as caught:
                audit._read_runtime_owner()
            assert caught.value.code == "action_runtime_owner_required"
        else:
            assert audit._read_runtime_owner() is None
        assert audit._db.total_changes == changes and audit.runtime_fence is None


def test_actual_owner_read_is_only_select_and_uses_original_fence(tmp_path):
    with SQLiteActionAuditStore(tmp_path / "audit.db", require_runtime_owner=True) as audit:
        with audit.runtime_owner() as fence:
            changes, statements = audit._db.total_changes, []
            audit._db.set_trace_callback(statements.append)
            audit._db.set_authorizer(
                lambda action, *_: (
                    sqlite3.SQLITE_OK
                    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}
                    else sqlite3.SQLITE_DENY
                )
            )
            try:
                assert audit._read_runtime_owner() is fence
                assert audit._read_runtime_owner() is fence
                assert not audit._db.in_transaction
                assert audit._db.total_changes == changes
            finally:
                audit._db.set_authorizer(None)
                audit._db.set_trace_callback(None)
            assert len(statements) == 2
            assert all(statement.startswith("SELECT ") for statement in statements)
            assert all(fence.token not in statement for statement in statements)
        with pytest.raises(KernelError) as caught:
            audit._read_runtime_owner()
        assert caught.value.code == "action_runtime_owner_required"


@pytest.mark.parametrize("field", ["owner_generation", "owner_token_sha256"])
@pytest.mark.parametrize("change", ["replace", "delete"])
def test_readonly_fence_rejects_persistent_owner_drift(tmp_path, field, change):
    path = tmp_path / "audit.db"
    with SQLiteActionAuditStore(path, require_runtime_owner=True) as audit:
        with audit.runtime_owner():
            with sqlite3.connect(path) as external:
                if change == "delete":
                    external.execute("DELETE FROM action_audit_metadata WHERE key=?", (field,))
                else:
                    external.execute(
                        "UPDATE action_audit_metadata SET value=? WHERE key=?", ("changed", field)
                    )
            changes = audit._db.total_changes
            with pytest.raises(KernelError) as caught:
                audit._read_runtime_owner()
            assert caught.value.code == "action_runtime_fence_lost"
            assert audit._db.total_changes == changes and not audit._db.in_transaction


def test_terminal_read_accepts_readonly_fence_but_preserves_write_denial(tmp_path):
    visits = []
    with (
        SQLiteWorkspaceTransactionStore(tmp_path / "store") as store,
        SQLiteActionAuditStore(
            tmp_path / "audit.db",
            require_runtime_owner=True,
            checkpoint=lambda: visits.append(True),
        ) as audit,
    ):
        with audit.runtime_owner() as fence:
            changes = audit._db.total_changes
            with terminal_read_scope(store, audit, store._read_blob, lambda: None):
                assert audit._read_runtime_owner() is fence
                for operation in [audit._assert_runtime_owner, audit.runtime_owner]:
                    with pytest.raises(KernelError) as caught:
                        if operation == audit.runtime_owner:
                            with operation():
                                pytest.fail("末端读不得重新取得Owner")
                        else:
                            operation()
                    assert caught.value.code == "terminal_read_write_denied"
            assert audit._db.total_changes == changes and visits == []
            assert audit._assert_runtime_owner() is fence


def test_readonly_fence_does_not_reuse_previous_generation(tmp_path):
    with SQLiteActionAuditStore(tmp_path / "audit.db", require_runtime_owner=True) as audit:
        with audit.runtime_owner() as first:
            assert audit._read_runtime_owner() is first
        with audit.runtime_owner() as second:
            assert second.generation == first.generation + 1 and second.token != first.token
            assert audit._read_runtime_owner() is second
