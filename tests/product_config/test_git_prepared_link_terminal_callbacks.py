"""真实原认证 SDK 的末端无共享回调及认证后原 CAS 缺件回归。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import action_runtime
from harnessix.product_config import git_prepared_link_ledger as ledger_module
from harnessix.product_config import git_prepared_link_observation as observation
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.product_config.test_git_prepared_link_controls import (
    _business_unchanged,
    _persisted,
    _sealed,
)
from tests.product_config.test_git_prepared_link_ledger import _case, _database, _ledger, _rows


def _instrument_constructors(monkeypatch):
    """只在装配构造器参数中链式注入；保留原回调，不替换实际 Store 或共享属性。"""
    calls = dict.fromkeys(("transactions", "audit", "audit_blob"), 0)
    phase, created, terminal_calls = {"terminal": False}, {}, {}

    def callback(name, original):
        def checkpoint():
            calls[name] += 1
            if phase["terminal"]:
                terminal_calls[name] = terminal_calls.get(name, 0) + 1
            if original is not None:
                original()

        return checkpoint

    def transactions(*args, **kwargs):
        checkpoint = callback("transactions", kwargs.pop("checkpoint", None))
        store = SQLiteWorkspaceTransactionStore(*args, checkpoint=checkpoint, **kwargs)
        created["transactions"] = (store, checkpoint)
        return store

    def audit(*args, **kwargs):
        checkpoint = callback("audit", kwargs.pop("checkpoint", None))
        original_read = kwargs.pop("read_blob")

        def read_blob(digest):
            calls["audit_blob"] += 1
            if phase["terminal"]:
                terminal_calls["audit_blob"] = terminal_calls.get("audit_blob", 0) + 1
            return original_read(digest)

        store = SQLiteActionAuditStore(*args, checkpoint=checkpoint, read_blob=read_blob, **kwargs)
        created["audit"] = (store, checkpoint, read_blob, original_read)
        return store

    monkeypatch.setattr(action_runtime, "SQLiteWorkspaceTransactionStore", transactions)
    monkeypatch.setattr(action_runtime, "SQLiteActionAuditStore", audit)
    return created, calls, phase, terminal_calls


async def _operation(actual, ledger, operation, checkpoint):
    if operation == "read":
        return await ledger.read_all(cancel=CancelToken(), checkpoint=checkpoint)
    assert operation == "prepare"
    return await ledger.prepare(
        actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=checkpoint
    )


@pytest.mark.parametrize(("fmt", "continuous"), [("sha1", False), ("sha256", True)])
async def test_actual_sdk_terminal_never_calls_shared_callbacks_and_restores_normal_reads(
    tmp_path, config, monkeypatch, fmt, continuous
):
    created, calls, phase, terminal_calls = _instrument_constructors(monkeypatch)

    async def inspect(actual):
        scenario = actual.scenario
        transactions, transaction_callback = created["transactions"]
        audit, audit_callback, audit_reader, original_reader = created["audit"]
        strict_reader = SQLiteWorkspaceTransactionStore._read_blob

        def original_identities():
            assert type(transactions) is SQLiteWorkspaceTransactionStore
            assert type(audit) is SQLiteActionAuditStore
            assert scenario.transactions is actual.preparer.core_store.store is transactions
            assert scenario.router._audit is audit
            assert transactions._checkpoint is transaction_callback
            assert audit._checkpoint is audit_callback
            assert audit._read_blob is audit_reader
            assert original_reader.__self__ is transactions
            assert original_reader.__func__ is SQLiteWorkspaceTransactionStore.blob
            assert "_read_blob" not in vars(transactions)
            assert transactions._read_blob.__self__ is transactions
            assert transactions._read_blob.__func__ is strict_reader

        with _business_unchanged(actual):
            async with _database(actual) as db:
                original_identities()
                link, sealed = await _sealed(actual, db)
                assert len(link.plan.core.baseline.source.patches) == (2 if continuous else 1)
                core_body = transactions.blob(link.plan.core.fingerprint)
                authenticate = ledger_module._authenticate
                terminal = observation.PreparedLinkReadSet.terminal
                verify = observation.verify_prepared_link_terminal
                marker = KernelError("test_terminal_callback_abort", "测试末端读取异常退出")
                authenticated, verified, loops = [], [], []
                mode = {"abort": False}

                async def authenticated_original(*args, **kwargs):
                    before = calls.copy()
                    evidence = await authenticate(*args, **kwargs)
                    # 普通认证必须仍消费两个原构造检查点及原 Audit Reader。
                    assert not phase["terminal"]
                    assert all(calls[name] > before[name] for name in calls)
                    authenticated.append(evidence.link.plan.route.execution.plan_id)
                    return evidence

                def terminal_loop(read_set, *args, **kwargs):
                    assert not phase["terminal"]
                    assert tuple(read_set.evidence) == (actual.route.plan.execution.plan_id,)
                    loops.append(tuple(read_set.evidence))
                    phase["terminal"] = True
                    try:
                        original_identities()
                        return terminal(read_set, *args, **kwargs)
                    finally:
                        phase["terminal"] = False
                        original_identities()

                def verified_original(evidence, *args, **kwargs):
                    assert phase["terminal"]
                    original_identities()
                    verify(evidence, *args, **kwargs)
                    verified.append(evidence.link.plan.route.execution.plan_id)
                    if mode["abort"]:
                        raise marker

                def checkpoint():
                    if phase["terminal"]:
                        name = "ledger_checkpoint"
                        terminal_calls[name] = terminal_calls.get(name, 0) + 1

                with monkeypatch.context() as patch:
                    patch.setattr(ledger_module, "_authenticate", authenticated_original)
                    patch.setattr(observation.PreparedLinkReadSet, "terminal", terminal_loop)
                    patch.setattr(observation, "verify_prepared_link_terminal", verified_original)
                    for abort in (False, True):
                        mode["abort"] = abort
                        for operation in ("read", "prepare"):
                            authenticated.clear()
                            verified.clear()
                            loops.clear()
                            db.execute("BEGIN IMMEDIATE")
                            changes = db.total_changes
                            ledger = _ledger(actual, db)
                            if abort:
                                with pytest.raises(KernelError) as caught:
                                    await _operation(actual, ledger, operation, checkpoint)
                                assert caught.value is marker
                            else:
                                result = await _operation(actual, ledger, operation, checkpoint)
                                assert result == ((link,) if operation == "read" else link)
                            assert len(authenticated) == (1 if operation == "read" else 2)
                            assert loops == [(actual.route.plan.execution.plan_id,)]
                            assert verified == [actual.route.plan.execution.plan_id]
                            assert db.in_transaction and _rows(db) == sealed
                            assert db.total_changes == changes
                            db.execute("ROLLBACK")
                            await _persisted(actual, sealed)
                            original_identities()

                            # 正常和异常退出后普通读都重新进入原构造回调，不能留下共享静默。
                            before = calls.copy()
                            assert transactions.blob(link.plan.core.fingerprint) == core_body
                            assert calls["transactions"] > before["transactions"]
                            before = calls.copy()
                            assert (
                                scenario.router.status(actual.route.plan.execution.plan_id)
                                == actual.route
                            )
                            assert all(calls[name] > before[name] for name in calls)

                assert not phase["terminal"]
                assert not terminal_calls, f"末端重入共享回调：{terminal_calls}"

    await _case(tmp_path, config, monkeypatch, inspect, fmt=fmt, continuous=continuous)


async def test_actual_sdk_missing_core_source_and_route_parents_after_last_authentication_reject(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _business_unchanged(actual):
            async with _database(actual) as db:
                link, sealed = await _sealed(actual, db)
                store = actual.scenario.transactions
                assert type(store) is SQLiteWorkspaceTransactionStore
                assert type(actual.scenario.router._audit) is SQLiteActionAuditStore
                core = link.plan.core
                faults = (
                    ("core", core.fingerprint),
                    ("source_parent", core.baseline.source.workspace.parent_closure.sha256),
                    ("route_parent", link.plan.route.execution.workspace.parent_closure.sha256),
                )
                authenticate = ledger_module._authenticate
                verify = observation.verify_prepared_link_terminal
                covered = []
                for fault, digest in faults:
                    path = store._blobs / digest
                    missing = path.with_name(path.name + ".terminal-callback-missing")
                    body = path.read_bytes()
                    for operation in ("read", "prepare"):
                        visits = {"authenticated": 0, "terminal": 0, "injected": False}
                        last_authentication = 1 if operation == "read" else 2

                        async def after_last_authentication(
                            *args,
                            visits=visits,
                            last_authentication=last_authentication,
                            path=path,
                            missing=missing,
                            **kwargs,
                        ):
                            evidence = await authenticate(*args, **kwargs)
                            visits["authenticated"] += 1
                            if visits["authenticated"] == last_authentication:
                                # 原完整异步认证已经返回；仅移除真实原 CAS 文件，不改认证结果。
                                assert not missing.exists()
                                path.rename(missing)
                                visits["injected"] = True
                            return evidence

                        def terminal_original(evidence, *args, visits=visits, path=path, **kwargs):
                            assert visits["injected"] and not path.exists()
                            visits["terminal"] += 1
                            return verify(evidence, *args, **kwargs)

                        async with _database(actual, read_only=operation == "read") as caller:
                            caller.execute("BEGIN" if operation == "read" else "BEGIN IMMEDIATE")
                            changes = caller.total_changes
                            try:
                                with monkeypatch.context() as patch:
                                    patch.setattr(
                                        ledger_module, "_authenticate", after_last_authentication
                                    )
                                    patch.setattr(
                                        observation,
                                        "verify_prepared_link_terminal",
                                        terminal_original,
                                    )
                                    with pytest.raises(KernelError):
                                        await _operation(
                                            actual, _ledger(actual, caller), operation, lambda: None
                                        )
                                assert visits == {
                                    "authenticated": last_authentication,
                                    "terminal": 1,
                                    "injected": True,
                                }
                                assert caller.in_transaction and _rows(caller) == sealed
                                assert caller.total_changes == changes
                                assert not path.exists() and missing.read_bytes() == body
                                covered.append((fault, operation))
                            finally:
                                if caller.in_transaction:
                                    caller.execute("ROLLBACK")
                                if visits["injected"]:
                                    missing.rename(path)
                            assert store.blob(digest) == body
                            await _persisted(actual, sealed)
                assert covered == [(fault, op) for fault, _ in faults for op in ("read", "prepare")]

    await _case(tmp_path, config, monkeypatch, inspect)
