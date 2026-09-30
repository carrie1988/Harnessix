"""正式产品回滚使用真实Gateway、Agent、SQLite和Workspace，不替换文件执行器。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _approval, _proposal


@asynccontextmanager
async def product(tmp_path, *, enabled=True):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src/modified.py").write_text("old\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests/deleted.txt").write_text("remove\n", encoding="utf-8")
    environment = build_fixed_product_action_environment(root)
    plans = SQLiteExecutionPlanStore(tmp_path / "state/plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "state/audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "state/delivery")
    leases = WorkspaceLeaseStore(tmp_path / "state/leases.db")
    sessions = SQLiteSessionStore(tmp_path / "state/sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    router = TrustedActionRouter(
        plans=plans, audit=audit, workspace_root=environment.workspace_root
    )
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        composition = build_product_action_composition(
            build_product_action_config(workspace_patch_enabled=enabled),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=tools.workspace_scope,
        )
        provider = ScriptedProvider([])
        try:
            async with AgentRuntime(
                sessions,
                provider,
                trusted_actions=composition.gateway,
                artifacts=artifacts,
                scoped_tools=tools,
            ) as runtime:
                yield root, runtime, provider, router, transactions, composition, artifacts
        finally:
            if composition.gateway is not None:
                composition.gateway.close()
            plans.close()
            audit.close()
            transactions.close()
            leases.close()


async def test_product_advertises_rollback_with_same_patch_capability_gate(tmp_path):
    async with product(tmp_path) as (_, _, _, router, _, composition, _):
        assert "rollback_workspace_patch" in {
            item.name for item in composition.catalog.definitions()
        }
        assert "rollback_workspace_patch" in {item.tool for item in router.bindings()}


async def test_doctor_and_runtime_capture_identical_rollback_capabilities(tmp_path, monkeypatch):
    from harnessix.domain.models import utc_now
    from harnessix.product_config.action_diagnostics import diagnose_product_actions
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource

    # 报告摘要包含探测时间；固定同一次探测时刻后比较全部字段，不省略身份或有效期。
    now = utc_now()
    monkeypatch.setattr("harnessix.product_config.action_contracts.utc_now", lambda: now)
    async with product(tmp_path) as (root, _, _, _, _, composition, _):
        secrets = EnvironmentSecretProvider(
            [EnvironmentSecretSource("offline", "v1", "HARNESSIX_OFFLINE")],
            environment={},
        )
        diagnosed = diagnose_product_actions(
            build_product_action_config(),
            platform=composition.report.capabilities[0].platform,
            secrets=secrets,
            workspace=root,
        )
        assert diagnosed == composition.report


def rollback_step(transaction_id):
    return [
        ResponseStarted(response_id="rollback-response"),
        ToolCallCompleted(
            call_id="rollback-call",
            tool="rollback_workspace_patch",
            arguments={"transaction_id": str(transaction_id)},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def result(turn):
    return next(item.content for item in turn.items if isinstance(item.content, ToolResultContent))


async def approve(runtime, thread_id, waiting, *, outcome=ApprovalOutcome.APPROVED):
    request = _approval(waiting)
    await runtime.reply_approval(
        thread_id,
        waiting.turn_id,
        request.approval_id,
        fingerprint=request.request_fingerprint,
        decision=ApprovalDecision(outcome=outcome, actor="reviewer"),
    )
    return await runtime.resume_turn(thread_id, waiting.turn_id)


async def publish(runtime, provider, root):
    provider.steps = (_action_step(_proposal()), answer("修改完成"))
    thread = await runtime.create_thread(str(root))
    waiting = await runtime.run_turn(thread.thread_id, "修改三个文件", request_id="patch")
    completed = await approve(runtime, thread.thread_id, waiting)
    assert result(completed).outcome == "succeeded"
    return thread.thread_id, _approval(waiting).plan_id


async def test_rollback_three_operations_has_new_diff_approval_and_original_unchanged(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, artifacts):
        thread_id, original_id = await publish(runtime, provider, root)
        original = transactions.load(original_id)
        original_events = transactions._db.execute(
            "SELECT * FROM workspace_transaction_events WHERE transaction_id = ? ORDER BY sequence",
            (str(original_id),),
        ).fetchall()
        provider.steps = (rollback_step(original_id), answer("回滚完成"))
        waiting = await runtime.run_turn(thread_id, "撤销上次修改", request_id="rollback")
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        request = _approval(waiting)
        assert request.plan_id != original_id and request.diff_artifact is not None
        assert request.presentation == "patch_batch"
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        page = await artifacts.read(
            thread_id, runtime.tools.workspace_scope, request.diff_artifact.artifact_id, limit=200
        )
        assert page.next_offset is None
        assert "modified.py" in page.model_dump_json()
        assert "新增.py" in page.model_dump_json()
        assert "deleted.txt" in page.model_dump_json()
        completed = await approve(runtime, thread_id, waiting)
        output = result(completed)
        assert output.outcome == "succeeded"
        assert output.output["transaction_id"] == str(request.plan_id)
        assert output.output["files"] == 3
        assert router.status(request.plan_id).state == "succeeded"
        assert (root / "src/modified.py").read_bytes() == b"old\n"
        assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
        assert not (root / "src/新增.py").exists()
        assert transactions.load(original_id) == original
        assert (
            transactions._db.execute(
                "SELECT * FROM workspace_transaction_events "
                "WHERE transaction_id = ? ORDER BY sequence",
                (str(original_id),),
            ).fetchall()
            == original_events
        )


@pytest.mark.parametrize("target", ["unknown", "other-thread", "fork"])
async def test_unowned_rollback_never_reads_original_blob(tmp_path, monkeypatch, target):
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        owner, original_id = await publish(runtime, provider, root)
        if target == "unknown":
            selected, original_id = owner, uuid4()
        elif target == "fork":
            selected = (await runtime.fork_thread(owner, request_id="fork")).thread_id
        else:
            selected = (await runtime.create_thread(str(root))).thread_id

        def forbidden_blob(_digest):
            pytest.fail("归属拒绝前不能读取其他事务Blob")

        monkeypatch.setattr(transactions, "blob", forbidden_blob)
        provider.steps = (rollback_step(original_id), answer("请求未获批准"))
        completed = await runtime.run_turn(selected, "撤销修改", request_id="unowned")
        assert result(completed).outcome == "failed"
        assert result(completed).error.code == "workspace_rollback_not_owned"
        assert (root / "src/modified.py").read_bytes() == b"new\n"


@pytest.mark.parametrize("change", ["replace", "created", "deleted", "mode"])
async def test_third_content_is_preserved_and_no_inverse_plan_saved(tmp_path, change):
    import os

    if change == "mode" and os.name == "nt":
        pytest.skip("Windows普通NTFS不模拟POSIX可执行模式")
    async with product(tmp_path) as (root, runtime, provider, _, transactions, _, _):
        thread_id, original_id = await publish(runtime, provider, root)
        leaf = root / (
            "src/新增.py"
            if change == "created"
            else "tests/deleted.txt"
            if change == "deleted"
            else "src/modified.py"
        )
        if change == "mode":
            leaf.chmod(0o755)
        else:
            leaf.write_bytes(b"user edit\n")
        before = leaf.read_bytes(), leaf.stat().st_mode
        provider.steps = (rollback_step(original_id), answer("保留冲突内容"))
        completed = await runtime.run_turn(thread_id, "撤销", request_id="conflict")
        assert result(completed).outcome == "failed"
        assert (leaf.read_bytes(), leaf.stat().st_mode) == before
        assert transactions.load(original_id).state == "published"
        from harnessix.agent.approvals import trusted_action_invocation_id
        from harnessix.agent.models import ToolCallContent

        call = next(
            item.content for item in completed.items if isinstance(item.content, ToolCallContent)
        )
        from harnessix.agent.errors import KernelError

        with pytest.raises(KernelError) as missing:
            transactions.load(trusted_action_invocation_id(thread_id, completed.turn_id, call))
        assert missing.value.code == "delivery_transaction_not_found"


@pytest.mark.parametrize("operation", ["reject", "cancel", "post-approval-drift"])
async def test_waiting_rollback_and_approved_drift_do_not_overwrite(tmp_path, operation):
    async with product(tmp_path) as (root, runtime, provider, _, _, _, _):
        thread_id, original_id = await publish(runtime, provider, root)
        provider.steps = (rollback_step(original_id), answer("未执行回滚"))
        waiting = await runtime.run_turn(thread_id, "撤销", request_id=operation)
        assert waiting.status is TurnStatus.WAITING_APPROVAL
        if operation == "cancel":
            stopped = await runtime.cancel(thread_id, waiting.turn_id)
            assert stopped.status is TurnStatus.CANCELLED
        elif operation == "reject":
            await approve(runtime, thread_id, waiting, outcome=ApprovalOutcome.REJECTED)
        else:
            (root / "src/modified.py").write_bytes(b"later edit\n")
            completed = await approve(runtime, thread_id, waiting)
            assert result(completed).outcome != "succeeded"
        assert (root / "src/modified.py").read_bytes() == (
            b"later edit\n" if operation == "post-approval-drift" else b"new\n"
        )


async def test_disabled_patch_removes_rollback_binding_and_advertisement(tmp_path):
    async with product(tmp_path, enabled=False) as (_, _, _, router, _, composition, _):
        assert composition.catalog.definitions() == ()
        assert router.bindings() == ()
        assert {item.capability_id for item in composition.report.capabilities} == {
            "apply_patch_batch",
            "rollback_workspace_patch",
        }
        assert all(item.status == "omitted" for item in composition.report.capabilities)


async def test_second_rollback_is_known_conflict_without_pending_approval(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, _, _, _):
        thread_id, original_id = await publish(runtime, provider, root)
        provider.steps = (rollback_step(original_id), answer("回滚完成"))
        waiting = await runtime.run_turn(thread_id, "撤销", request_id="rollback-once")
        await approve(runtime, thread_id, waiting)
        provider.steps = (rollback_step(original_id), answer("原修改已经撤销"))
        completed = await runtime.run_turn(thread_id, "再次撤销", request_id="rollback-twice")
        output = result(completed)
        assert output.outcome == "failed"
        assert output.error.code == "workspace_rollback_conflict"
        assert output.trusted_action is None
        from harnessix.agent.approvals import trusted_action_invocation_id
        from harnessix.agent.models import ToolCallContent

        call = next(
            item.content for item in completed.items if isinstance(item.content, ToolCallContent)
        )
        assert (
            router.status(trusted_action_invocation_id(thread_id, completed.turn_id, call)).state
            == "denied"
        )
        assert (root / "src/modified.py").read_bytes() == b"old\n"
        assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
        assert not (root / "src/新增.py").exists()


async def test_cancel_unapproved_original_patch_has_no_effect_and_closes_route(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        provider.steps = (_action_step(_proposal()),)
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改", request_id="cancel-patch")
        request = _approval(waiting)
        stopped = await runtime.cancel(thread.thread_id, waiting.turn_id)
        assert stopped.status is TurnStatus.CANCELLED
        output = result(stopped)
        assert output.outcome == "cancelled" and output.trusted_action is None
        assert router.status(request.plan_id).state == "denied"
        assert transactions.load(request.plan_id).state == "prepared"
        assert (root / "src/modified.py").read_bytes() == b"old\n"
        assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
        assert not (root / "src/新增.py").exists()


@pytest.mark.parametrize("field", ["workspace", "files", "approved", "mode"])
def test_rollback_input_rejects_caller_supplied_authority(field):
    from harnessix.agent.errors import KernelError
    from harnessix.delivery.rollback_action import decode_workspace_rollback_input

    with pytest.raises(KernelError) as invalid:
        decode_workspace_rollback_input({"transaction_id": str(uuid4()), field: "forbidden"})
    assert invalid.value.code == "workspace_rollback_arguments_invalid"


@pytest.mark.parametrize(
    "before",
    [b"x" * 600_000 + b"\n", b"\x00\xff" * 300_000],
    ids=["large-text", "binary"],
)
async def test_rollback_restores_original_large_or_binary_bytes_without_model_body(
    tmp_path, before
):
    import hashlib

    from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput

    async with product(tmp_path) as (root, runtime, provider, _, _, _, _):
        leaf = root / "src/modified.py"
        leaf.write_bytes(before)
        proposal = WorkspacePatchInput(
            files=(
                WorkspacePatchFile(
                    path="src/modified.py",
                    operation="replace",
                    expected_sha256=hashlib.sha256(before).hexdigest(),
                    content="short\n",
                    mode=0o644,
                ),
            )
        )
        provider.steps = (_action_step(proposal), answer("修改完成"))
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "替换文件", request_id="large-patch")
        completed = await approve(runtime, thread.thread_id, waiting)
        assert result(completed).outcome == "succeeded"
        provider.steps = (rollback_step(_approval(waiting).plan_id), answer("回滚完成"))
        waiting = await runtime.run_turn(
            thread.thread_id, "恢复原字节", request_id="large-rollback"
        )
        completed = await approve(runtime, thread.thread_id, waiting)
        assert result(completed).outcome == "succeeded"
        assert leaf.read_bytes() == before
