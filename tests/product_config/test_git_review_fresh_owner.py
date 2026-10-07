"""真实SDK的Owner隐式快照反例与短只读观察连接生命周期。"""

import sqlite3
from contextlib import closing

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config import git_delivery_review_host as hosts
from tests.product_config.test_git_prepared_link_ledger import _case
from tests.product_config.test_git_review_runtime_fence import _host, _rejection


async def test_actual_host_rejects_implicit_snapshot_at_construction_and_recheck(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        audit = actual.scenario.router._audit
        check = _host(actual)
        for field in ["owner_generation", "owner_token_sha256"]:
            saved = audit._db.execute(
                "SELECT value FROM action_audit_metadata WHERE key=?", (field,)
            ).fetchone()
            cursor = audit._db.execute("SELECT key,value FROM action_audit_metadata ORDER BY key")
            assert cursor.fetchone() is not None
            try:
                with closing(sqlite3.connect(audit._path)) as external, external:
                    external.execute(
                        "UPDATE action_audit_metadata SET value='changed' WHERE key=?", (field,)
                    )
                assert not audit._db.in_transaction
                assert (
                    audit._db.execute(
                        "SELECT value FROM action_audit_metadata WHERE key=?", (field,)
                    ).fetchone()
                    == saved
                )
                assert _rejection(check) == "action_runtime_fence_lost"
                assert _rejection(lambda: _host(actual)) == "action_runtime_fence_lost"
            finally:
                cursor.close()
                audit._db.execute(
                    "UPDATE action_audit_metadata SET value=? WHERE key=?", (saved[0], field)
                )
            check()

    await _case(tmp_path, config, monkeypatch, inspect, explicit_git_ledger=False)


async def test_actual_fresh_owner_observers_are_readonly_closed_and_fail_closed(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        audit = actual.scenario.router._audit
        original, changes = audit._db, audit._db.total_changes
        views, sql = [], []
        factory = hosts.readonly_database

        def observe(path):
            assert path == audit._path
            view = factory(path)
            assert view.execute("PRAGMA query_only").fetchone() == (1,)
            view.set_trace_callback(sql.append)
            views.append(view)
            return view

        with monkeypatch.context() as patch:
            patch.setattr(hosts, "readonly_database", observe)
            check = _host(actual)
            check()
        assert len(views) == len(sql) == 2
        assert all(statement.startswith("SELECT ") for statement in sql)
        assert audit._db is original and audit._db.total_changes == changes
        for view in views:
            with pytest.raises(sqlite3.ProgrammingError):
                view.execute("SELECT 1")

        # 原连接或原Owner检查先失败时，不能用观察连接修复或代替它。
        count = len(views)
        with monkeypatch.context() as patch:
            patch.setattr(hosts, "readonly_database", observe)
            patch.setattr(audit, "_require_runtime_owner", False)
            assert _rejection(check) == "git_action_review_host_invalid"
        assert len(views) == count
        with monkeypatch.context() as patch:
            patch.setattr(hosts, "readonly_database", lambda path: original)
            assert _rejection(check) == "git_action_review_host_invalid"
        assert audit._db is original and original.execute("SELECT 1").fetchone() == (1,)

        with closing(sqlite3.connect(audit._path)) as replacement:

            def return_frozen_original_after_rebinding(path):
                audit._db = replacement
                return original

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(
                        hosts, "readonly_database", return_frozen_original_after_rebinding
                    )
                    assert _rejection(check) == "git_action_review_host_invalid"
            finally:
                audit._db = original
        assert original.execute("SELECT 1").fetchone() == (1,)

        class ObserverSubclass(sqlite3.Connection):
            def close(self):
                pytest.fail("拒绝子类时不得调用可覆盖的close")

        subclass = sqlite3.connect(":memory:", factory=ObserverSubclass)
        with monkeypatch.context() as patch:
            patch.setattr(hosts, "readonly_database", lambda path: subclass)
            assert _rejection(check) == "git_action_review_host_invalid"
        with pytest.raises(sqlite3.ProgrammingError):
            subclass.execute("SELECT 1")

        # 观察SQL失败必须关闭本次新视图，保留原连接和业务状态。
        broken = sqlite3.connect(":memory:")
        with monkeypatch.context() as patch:
            patch.setattr(hosts, "readonly_database", lambda path: broken)
            assert _rejection(check) == "git_action_review_host_invalid"
        with pytest.raises(sqlite3.ProgrammingError):
            broken.execute("SELECT 1")

        saved = original.execute(
            "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
        ).fetchone()

        def changed_after_original_read(path):
            with closing(sqlite3.connect(path)) as external, external:
                external.execute(
                    "UPDATE action_audit_metadata SET value='changed' WHERE key='owner_generation'"
                )
            return observe(path)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(hosts, "readonly_database", changed_after_original_read)
                assert _rejection(check) == "action_runtime_fence_lost"
        finally:
            original.execute(
                "UPDATE action_audit_metadata SET value=? WHERE key='owner_generation'", saved
            )
        with pytest.raises(sqlite3.ProgrammingError):
            views[-1].execute("SELECT 1")

        def unavailable(path):
            raise sqlite3.OperationalError("private-database-location")

        with monkeypatch.context() as patch:
            patch.setattr(hosts, "readonly_database", unavailable)
            with pytest.raises(KernelError) as caught:
                check()
            assert caught.value.code == "git_action_review_host_invalid"
            assert "private-database-location" not in str(caught.value)
        check()
        assert audit._db is original and not original.in_transaction

    await _case(tmp_path, config, monkeypatch, inspect, explicit_git_ledger=False)
