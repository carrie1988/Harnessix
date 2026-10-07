"""短只读Owner观察的SQL、初始化失败清理及文件观察门禁。"""

import sqlite3

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_delivery_review_host as hosts
from harnessix.sqlite_readonly import readonly_database
from harnessix.trusted_actions.store import SQLiteActionAuditStore


def test_fresh_reader_runs_only_original_connection_pragmas_and_owner_select(tmp_path, monkeypatch):
    path = tmp_path / "真实 #% Audit.db"
    connect, connections, statements = sqlite3.connect, [], []

    def observed_connect(address, **kwargs):
        assert address == path.absolute().as_uri() + "?mode=ro"
        assert kwargs == {"uri": True, "timeout": 0.1}
        database = connect(address, **kwargs)
        database.set_trace_callback(statements.append)
        connections.append(database)
        return database

    with SQLiteActionAuditStore(path, require_runtime_owner=True) as audit:
        with audit.runtime_owner():
            changes = audit._db.total_changes
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", observed_connect)
                hosts._read_fresh_owner(
                    audit, path, hosts._audit_file_identity(path), original=audit._db
                )
            assert len(connections) == 1
            assert statements[:2] == ["PRAGMA query_only = ON", "PRAGMA foreign_keys = ON"]
            assert len(statements) == 3 and statements[2].startswith("SELECT ")
            assert audit._db.total_changes == changes and not audit._db.in_transaction
            with pytest.raises(sqlite3.ProgrammingError):
                connections[0].execute("SELECT 1")


@pytest.mark.parametrize("failure_at", [1, 2])
def test_readonly_initialization_failure_closes_new_connection(tmp_path, monkeypatch, failure_at):
    path = tmp_path / "audit.db"
    connect, opened = sqlite3.connect, []

    def failing_connect(*args, **kwargs):
        database = connect(*args, **kwargs)
        opened.append(database)
        calls = 0

        def authorize(action, *_):
            nonlocal calls
            if action == sqlite3.SQLITE_PRAGMA:
                calls += 1
                if calls == failure_at:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        database.set_authorizer(authorize)
        return database

    with SQLiteActionAuditStore(path):
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", failing_connect)
            with pytest.raises(sqlite3.DatabaseError):
                readonly_database(path)
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


def test_missing_directory_or_observed_replacement_is_rejected_before_sql(tmp_path, monkeypatch):
    path = tmp_path / "leaf.db"
    path.write_bytes(b"original")
    identity = hosts._audit_file_identity(path)
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"replacement")
    replacement.replace(path)
    calls = []
    monkeypatch.setattr(hosts, "readonly_database", lambda *_: calls.append(True))
    with pytest.raises(KernelError) as caught:
        hosts._read_fresh_owner(None, path, identity, original=None)
    assert caught.value.code == "git_action_review_host_invalid" and calls == []
    for invalid in [tmp_path / "missing.db", tmp_path]:
        with pytest.raises(KernelError) as caught:
            hosts._audit_file_identity(invalid)
        assert caught.value.code == "git_action_review_host_invalid"
