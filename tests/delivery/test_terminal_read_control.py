"""原 Store 的末端控制与上下文隔离；不证明 prepared 业务认证或 Git 执行。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.terminal_read_control import (
    require_terminal_read_scope,
    run_store_read_checkpoint,
    terminal_parent_reader,
    terminal_read_scope,
)
from tests.delivery.test_git_material_cas import _material
from tests.trusted_actions.test_parent_closure_store import parent_route


def test_original_post_read_callback_cannot_remove_terminal_material(tmp_path):
    """原 CAS 正常读的删后回调反例与末端路径对照，不替换原 Blob IO。"""
    material = _material()
    path = tmp_path / "store" / "blobs" / material.body_sha256
    armed, visits = False, []

    def callback():
        if armed:
            visits.append(True)
            if len(visits) == 2:
                path.unlink()

    with SQLiteWorkspaceTransactionStore(tmp_path / "store", checkpoint=callback) as store:
        cas = GitMaterialCAS(store)
        reference = cas.persist(material)
        armed = True
        # 保留原正常 API 行为的具体反例；它不是终端读取的完成证明。
        assert cas.read(reference) == material
        assert len(visits) == 2 and not path.exists()
        armed = False
        store.put_blob(reference.cas_digest, material.body)
        visits.clear()
        armed = True
        with terminal_read_scope(store, object(), store._read_blob, lambda: None):
            assert cas.read(reference) == material
            assert path.exists() and not visits
            assert store._checkpoint is callback
        assert cas.read(reference) == material
        assert len(visits) == 2 and not path.exists()


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("kind", ["cancel", "deadline", "same_io_code"])
def test_terminal_cas_preserves_exact_internal_control_exception(tmp_path, phase, kind):
    token = CancelToken()
    token.cancel()
    try:
        token.checkpoint()
    except TurnCancelled as error:
        cancelled = error
    failure = {
        "cancel": cancelled,
        "deadline": KernelError("git_prepared_link_timeout", "期限测试"),
        "same_io_code": KernelError("git_material_cas_read_failed", "控制来源测试"),
    }[kind]
    armed, visits = False, []

    def control():
        if armed:
            visits.append(True)
            if len(visits) == (1 if phase == "before" else 2):
                raise failure

    with SQLiteWorkspaceTransactionStore(tmp_path / "store") as store:
        cas = GitMaterialCAS(store)
        reference = cas.persist(_material())
        with terminal_read_scope(store, object(), store._read_blob, control):
            armed = True
            with pytest.raises(type(failure)) as caught:
                cas.read(reference)
            assert caught.value is failure
            armed = False


def test_terminal_scope_rejects_writes_nesting_and_restores_after_failure(tmp_path):
    visits, controls = [], []
    with (
        SQLiteWorkspaceTransactionStore(
            tmp_path / "store", checkpoint=lambda: visits.append(True)
        ) as store,
        SQLiteActionAuditStore(tmp_path / "audit.db") as audit,
    ):
        failure = ValueError("terminal-fixture")
        with pytest.raises(ValueError) as caught:
            with terminal_read_scope(store, audit, store._read_blob, lambda: controls.append(True)):
                run_store_read_checkpoint(store, store._checkpoint)
                assert not visits and controls
                for write in (
                    lambda: store.put_blob("0" * 64, b""),
                    audit._assert_runtime_owner,
                ):
                    with pytest.raises(KernelError) as denied:
                        write()
                    assert denied.value.code == "terminal_read_write_denied"
                changes = audit._db.total_changes
                with pytest.raises(KernelError) as owner_denied:
                    with audit.runtime_owner():
                        pytest.fail("terminal runtime owner accepted")
                assert owner_denied.value.code == "terminal_read_write_denied"
                assert audit._db.total_changes == changes and audit.runtime_fence is None
                with pytest.raises(KernelError) as nested:
                    with terminal_read_scope(store, audit, store._read_blob, lambda: None):
                        pytest.fail("nested scope accepted")
                assert nested.value.code == "terminal_read_scope_invalid"
                raise failure
        assert caught.value is failure
        store.put_blob("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", b"")
        assert visits
        assert terminal_parent_reader(audit) is None


def test_original_audit_terminal_closure_uses_strict_cas_not_constructor_reader(tmp_path):
    """原 Route2 完整父闭包重读；构造 Reader/回调不能在末端修改已验材料。"""
    route, blobs = parent_route(tmp_path / "workspace")
    armed, visits = False, []
    with SQLiteWorkspaceTransactionStore(tmp_path / "store") as store:
        for digest, body in blobs.items():
            store.put_blob(digest, body)

        def original_reader(digest):
            visits.append("reader")
            assert not armed, "terminal constructor reader invoked"
            return store.blob(digest)

        def original_control():
            visits.append("control")
            assert not armed, "terminal constructor control invoked"

        with SQLiteActionAuditStore(
            tmp_path / "audit.db", read_blob=original_reader, checkpoint=original_control
        ) as audit:
            expected = audit.save_plan(route, initial_state="pending_approval")
            assert "reader" in visits and "control" in visits
            visits.clear()
            armed = True
            with terminal_read_scope(store, audit, store._read_blob, lambda: None):
                require_terminal_read_scope(store, audit)
                assert audit.load(route.execution.plan_id) == expected
                assert not visits
                assert audit._read_blob is original_reader and audit._checkpoint is original_control
                path = store._blobs / route.execution.workspace.parent_closure.sha256
                body = path.read_bytes()
                path.unlink()
                with pytest.raises(KernelError) as corrupted:
                    audit.load(route.execution.plan_id)
                assert corrupted.value.code == "action_audit_store_corrupt"
                path.write_bytes(body)
                path.chmod(0o600)
            armed = False
            assert audit.load(route.execution.plan_id) == expected
            assert "reader" in visits and "control" in visits
        with pytest.raises(KernelError) as missing:
            require_terminal_read_scope(store, audit)
        assert missing.value.code == "terminal_read_scope_invalid"


def test_original_audit_preserves_terminal_control_error_identity(tmp_path):
    route, blobs = parent_route(tmp_path / "workspace")
    with SQLiteWorkspaceTransactionStore(tmp_path / "store") as store:
        for digest, body in blobs.items():
            store.put_blob(digest, body)
        with SQLiteActionAuditStore(tmp_path / "audit.db", read_blob=store.blob) as audit:
            audit.save_plan(route, initial_state="pending_approval")
            armed = False
            failure = KernelError("action_audit_store_corrupt", "原内部控制测试")

            def control():
                if armed:
                    raise failure

            with terminal_read_scope(store, audit, store._read_blob, control):
                armed = True
                with pytest.raises(KernelError) as caught:
                    audit.load(route.execution.plan_id)
                assert caught.value is failure
                armed = False


def test_terminal_scope_rejects_other_thread_and_copied_context_after_exit(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "store") as store:
        with terminal_read_scope(store, object(), store._read_blob, lambda: None):
            context = copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(context.run, run_store_read_checkpoint, store, None)
                with pytest.raises(KernelError) as caught:
                    future.result()
                assert caught.value.code == "terminal_read_scope_invalid"
        with pytest.raises(KernelError) as expired:
            context.run(run_store_read_checkpoint, store, None)
        assert expired.value.code == "terminal_read_scope_invalid"


async def test_terminal_scope_rejects_inherited_child_task(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "store") as store:
        with terminal_read_scope(store, object(), store._read_blob, lambda: None):

            async def child():
                run_store_read_checkpoint(store, None)

            with pytest.raises(KernelError) as caught:
                await asyncio.create_task(child())
            assert caught.value.code == "terminal_read_scope_invalid"
            run_store_read_checkpoint(store, None)
