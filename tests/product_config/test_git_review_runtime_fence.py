"""真实认证宿主的原Audit代次复核；引用相同不代表持久Owner仍有效。"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_delivery_review_host import require_git_review_host
from harnessix.workspace.terminal_read_control import terminal_read_scope
from tests.product_config.test_git_prepared_link_ledger import _case


def _host(actual):
    scenario = actual.scenario
    return require_git_review_host(
        scenario.router,
        actual.preparer.core_store,
        actual.artifacts,
        scenario.reader,
        scenario.router._snapshot_ports,
        actual.provider._workspace_scope,
    )


def _rejection(check):
    try:
        check()
    except KernelError as error:
        return error.code
    return None


async def test_actual_host_rejects_original_owner_identity_and_persistent_drift(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        audit = actual.scenario.router._audit
        fence = audit._runtime_fence
        assert fence is not None and audit._require_runtime_owner is True
        check = _host(actual)
        observed = {}
        fields = [
            ("fence_copy", "_runtime_fence", fence.model_copy(deep=True)),
            ("fence_missing", "_runtime_fence", None),
            ("owner_optional", "_require_runtime_owner", False),
            ("closed", "_closed", True),
        ]
        for label, name, value in fields:
            with monkeypatch.context() as patch:
                patch.setattr(audit, name, value)
                observed[label] = _rejection(check)
        for name, value in {
            "generation": fence.generation + 1,
            "token": "0" * 64,
            "acquired_at": fence.acquired_at + timedelta(seconds=1),
        }.items():
            original = getattr(fence, name)
            object.__setattr__(fence, name, value)
            try:
                observed["fence_" + name] = _rejection(check)
            finally:
                object.__setattr__(fence, name, original)
        with sqlite3.connect(audit._path) as replacement:
            with monkeypatch.context() as patch:
                patch.setattr(audit, "_db", replacement)
                observed["_db"] = _rejection(check)
        for name in ["owner_generation", "owner_token_sha256"]:
            row = audit._db.execute(
                "SELECT value FROM action_audit_metadata WHERE key=?", (name,)
            ).fetchone()
            audit._db.execute(
                "UPDATE action_audit_metadata SET value='changed' WHERE key=?", (name,)
            )
            changes = audit._db.total_changes
            try:
                observed[name] = _rejection(check)
                assert audit._db.total_changes == changes
            finally:
                audit._db.execute(
                    "UPDATE action_audit_metadata SET value=? WHERE key=?", (row[0], name)
                )
        check()
        assert observed == {
            "fence_copy": "git_action_review_host_invalid",
            "fence_missing": "git_action_review_host_invalid",
            "owner_optional": "git_action_review_host_invalid",
            "closed": "git_user_observation_host_invalid",
            "fence_generation": "git_action_review_host_invalid",
            "fence_token": "git_action_review_host_invalid",
            "fence_acquired_at": "git_action_review_host_invalid",
            "_db": "git_action_review_host_invalid",
            "owner_generation": "action_runtime_fence_lost",
            "owner_token_sha256": "action_runtime_fence_lost",
        }

    await _case(tmp_path, config, monkeypatch, inspect, explicit_git_ledger=False)


async def test_actual_audit_pinned_read_snapshot_cannot_prove_current_owner(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        audit = actual.scenario.router._audit
        check = _host(actual)
        row = audit._db.execute(
            "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
        ).fetchone()
        audit._db.execute("BEGIN")
        assert (
            audit._db.execute(
                "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
            ).fetchone()
            == row
        )
        try:
            with sqlite3.connect(audit._path) as external:
                external.execute(
                    "UPDATE action_audit_metadata SET value='changed' WHERE key='owner_generation'"
                )
            assert (
                audit._db.execute(
                    "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
                ).fetchone()
                == row
            ), "真实WAL旧快照仍能返回已过期Owner"
            assert _rejection(check) == "git_action_review_host_invalid"
        finally:
            audit._db.execute("ROLLBACK")
            audit._db.execute(
                "UPDATE action_audit_metadata SET value=? WHERE key='owner_generation'", row
            )
        check()

    await _case(tmp_path, config, monkeypatch, inspect, explicit_git_ledger=False)


async def test_actual_host_terminal_fence_checks_read_only_and_never_call_shared_checkpoint(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        scenario = actual.scenario
        audit, store = scenario.router._audit, actual.preparer.core_store.store
        callbacks, statements = [], []
        changes = audit._db.total_changes
        with monkeypatch.context() as patch:
            patch.setattr(audit, "_checkpoint", lambda: callbacks.append(True))
            audit._db.set_trace_callback(statements.append)
            try:
                check = _host(actual)
                with terminal_read_scope(store, audit, store._read_blob, lambda: None):
                    check()
                    with pytest.raises(KernelError) as caught:
                        audit._assert_runtime_owner()
                    assert caught.value.code == "terminal_read_write_denied"
            finally:
                audit._db.set_trace_callback(None)
        assert not callbacks and audit._db.total_changes == changes
        assert len(statements) == 2 and all(s.startswith("SELECT ") for s in statements)
        assert all(audit._runtime_fence.token not in statement for statement in statements)

    await _case(tmp_path, config, monkeypatch, inspect, explicit_git_ledger=False)
