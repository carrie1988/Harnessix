from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalOutcome
from harnessix.patches import batch_agent_bridge, managed
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.diff_document_contracts import BatchDiffDocumentOptions
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.tools.workspace import ReadOperation
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.bridge_helpers import approval, make_scope
from tests.patches.test_diff_document import check
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case


def ledger_state(copy):
    return tuple(copy._db.iterdump())


async def test_real_plan_execution_reopen_history_without_writes(group_case, monkeypatch):
    source, factory, copy, _, prepared = group_case
    original = snapshot(source.root)
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        definition = bridge.definition()
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan)
        before = ledger_state(copy), snapshot(copy.workspace.root)
        planned = await bridge.diff(call, scope, plan, CancelToken())
        assert (ledger_state(copy), snapshot(copy.workspace.root)) == before
        assert planned.document.summary.view == "plan" and planned.document.summary.complete
        assert planned.approval is None and planned.execution is None
        result = await bridge.execute(call, scope, plan, decision, CancelToken())
        assert bridge.definition() == definition
    workspace_id = copy.workspace_id
    copy.close()
    with factory.open(workspace_id) as reopened:
        # 改变当前目标不能伪造或覆盖此前已归因的历史事实。
        (reopened.workspace.root / PATHS[0]).write_text("external")
        before = ledger_state(reopened), snapshot(reopened.workspace.root)
        async with ManagedPatchBatchBridge(reopened) as bridge:

            def forbidden(*args, **kwargs):
                pytest.fail("展示不得读目标或产生账本写")

            with monkeypatch.context() as m:
                m.setattr(reopened.workspace, "open", forbidden)
                for name in ("save", "reply", "execute", "reconcile", "verify"):
                    m.setattr(ManagedPatchBatches, name, forbidden)
                m.setattr(batch_agent_bridge, "prepare_patch_batch", forbidden)
                history = await bridge.diff(
                    call,
                    scope,
                    plan,
                    CancelToken(),
                    view="effect",
                    approval=decision,
                    execution=result.execution,
                )
                assert await bridge.diff(call, scope, plan, CancelToken()) == planned
                assert (
                    await bridge.diff(
                        call,
                        scope,
                        plan,
                        CancelToken(),
                        view="effect",
                        approval=decision,
                        execution=result.execution,
                    )
                    == history
                )
        assert (ledger_state(reopened), snapshot(reopened.workspace.root)) == before
        body = check(history.document).decode()
        assert (
            history.document.summary.effect == "applied" and history.execution == result.execution
        )
        assert tuple(e.path for e in history.document.edits) == PATHS
        for private in (
            str(plan.call_id),
            str(plan.thread_id),
            str(plan.turn_id),
            str(plan.backend.batch_id),
            str(plan.backend.workspace_id),
            plan.approval_fingerprint,
            decision.actor,
            str(reopened.workspace.root),
            *(str(m.plan_id) for m in plan.backend.members),
        ):
            assert private not in body and private not in repr(history)
        assert "before" not in repr(history) and "after" not in repr(history)
    assert snapshot(source.root) == original


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
async def test_partial_unknown_and_observed_history(group_case, monkeypatch, position, point):
    _, _, copy, _, prepared = group_case
    index = 0

    def fail(at):
        nonlocal index
        if at == point:
            index += 1
            if index == position + 1:
                raise OSError("报告前执行故障")

    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan)
        monkeypatch.setattr(managed, "_fault", fail)
        result = await bridge.execute(call, scope, plan, decision, CancelToken())
        before = ledger_state(copy), snapshot(copy.workspace.root)
        history = await bridge.diff(
            call,
            scope,
            plan,
            CancelToken(),
            view="effect",
            approval=decision,
            execution=result.execution,
        )
        assert (ledger_state(copy), snapshot(copy.workspace.root)) == before
        assert tuple(e.path for e in history.document.edits) == PATHS[:position]
        assert len(history.document.files) == 3
        assert history.document.summary.effect == result.execution.effect
        check(history.document)
        recovered = await bridge.recover(call, scope, CancelToken(), plan=plan, approval=decision)
        observed = await bridge.diff(
            call,
            scope,
            plan,
            CancelToken(),
            view="effect",
            approval=decision,
            execution=recovered.execution,
        )
        assert (
            tuple(e.path for e in observed.document.edits)
            == PATHS[: position + int(point == "after_replace")]
        )
        if point == "after_replace":
            with pytest.raises(KernelError) as error:
                await bridge.diff(
                    call,
                    scope,
                    plan,
                    CancelToken(),
                    view="effect",
                    approval=decision,
                    execution=result.execution,
                )
            assert error.value.code == "patch_diff_effect_mismatch"


@pytest.mark.parametrize("outcome", [ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED])
async def test_mirrored_decision_without_run_is_not_started(group_case, outcome):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan, outcome)
        groups.reply(
            plan.backend.batch_id,
            plan.backend.approval_fingerprint,
            bridge._decision(plan, decision),
            ReadOperation(),
        )
        before = ledger_state(copy), snapshot(copy.workspace.root)
        report = await bridge.diff(
            call, scope, plan, CancelToken(), view="effect", approval=decision
        )
        assert report.document.summary.phase == "not_started" and not report.document.edits
        assert report.document.summary.complete and len(report.document.files) == 3
        assert (ledger_state(copy), snapshot(copy.workspace.root)) == before
        check(report.document)


@pytest.mark.parametrize(
    "key", ["thread_id", "turn_id", "call_id", "workspace", "request_fingerprint"]
)
async def test_wrong_scope_is_rejected(group_case, key):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        value = (
            "/wrong"
            if key == "workspace"
            else "0" * 64
            if key == "request_fingerprint"
            else uuid4()
        )
        with pytest.raises(KernelError):
            await bridge.diff(call, replace(scope, **{key: value}), plan, CancelToken())


@pytest.mark.parametrize(
    "change",
    [
        "call",
        "definition",
        "plan",
        "approval",
        "missing_decision",
        "missing_run",
        "reordered_run",
        "wrong_run",
    ],
)
async def test_complete_call_approval_and_run_must_match(group_case, change):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan)
        result = await bridge.execute(call, scope, plan, decision, CancelToken())
        execution = result.execution
        if change == "call":
            call = call.model_copy(update={"call_id": uuid4()})
            scope = make_scope(copy, call)
        elif change == "definition":
            call = call.model_copy(update={"tool_version": "wrong"})
            scope = make_scope(copy, call)
        elif change == "plan":
            plan = plan.model_copy(update={"thread_id": uuid4()})
        elif change == "approval":
            decision = decision.model_copy(update={"actor": "other"})
        elif change == "missing_decision":
            decision = None
        elif change == "missing_run":
            execution = None
        elif change == "reordered_run":
            execution = execution.model_copy(update={"members": execution.members[::-1]})
        else:
            execution = execution.model_copy(
                update={"run": execution.run.model_copy(update={"batch_id": uuid4()})}
            )
        before = ledger_state(copy), snapshot(copy.workspace.root)
        with pytest.raises((KernelError, ValidationError)):
            await bridge.diff(
                call,
                scope,
                plan,
                CancelToken(),
                view="effect",
                approval=decision,
                execution=execution,
            )
        assert (ledger_state(copy), snapshot(copy.workspace.root)) == before


@pytest.mark.parametrize(
    "change",
    [
        "unmirrored",
        "unknown_view",
        "plan_with_decision",
        "corrupt_images",
        "missing_group",
        "small_budget",
    ],
)
async def test_report_fails_closed_without_repair_or_authorization(group_case, change):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        kwargs = {}
        if change == "unmirrored":
            kwargs = {"view": "effect", "approval": approval(plan)}
        elif change == "unknown_view":
            kwargs = {"view": "other"}
        elif change == "plan_with_decision":
            kwargs = {"approval": approval(plan)}
        elif change == "small_budget":
            kwargs = {"options": BatchDiffDocumentOptions(max_output_bytes=1024)}
        elif change == "missing_group":
            copy._db.execute("PRAGMA foreign_keys=OFF")
            copy._db.execute("DELETE FROM batches")
        else:
            copy._db.execute("UPDATE plans SET after_image = ?", (b"forged",))
        before = ledger_state(copy), snapshot(copy.workspace.root)
        with pytest.raises(KernelError):
            await bridge.diff(call, scope, plan, CancelToken(), **kwargs)
        assert (ledger_state(copy), snapshot(copy.workspace.root)) == before


@pytest.mark.parametrize("view", ["plan", "effect"])
async def test_document_cannot_bypass_existing_readonly_publisher(group_case, tmp_path, view):
    from harnessix.agent.models import ToolResultContent
    from harnessix.agent.runtime import AgentRuntime
    from harnessix.artifacts.contracts import ArtifactToolResult
    from harnessix.artifacts.sqlite import SQLiteArtifactStore
    from harnessix.models.scripted import ScriptedProvider
    from harnessix.session.sqlite import SQLiteSessionStore
    from tests.agent.helpers import answer
    from tests.patches.kernel_batch_helpers import approval_of, batch_step, decide
    from tests.patches.test_kernel_patch import results

    _, _, copy, _, prepared = group_case
    session = SQLiteSessionStore(tmp_path / "s.db")
    publisher = SQLiteArtifactStore(session)
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            session,
            ScriptedProvider([batch_step(copy, bridge, prepared), answer()]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "归档门禁", request_id="gate")
            request = approval_of(waiting)
            call = next(i.content for i in waiting.items if i.content.kind == "tool_call")
            scope = make_scope(copy, call, thread_id=thread.thread_id, turn_id=waiting.turn_id)
            kwargs = {}
            if view == "effect":
                decided = await decide(runtime, thread.thread_id, waiting)
                completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                kwargs = {
                    "view": view,
                    "approval": approval_of(decided).decision,
                    "execution": results(completed)[0].patch_batch.execution,
                }
            report = await bridge.diff(call, scope, request.plan, CancelToken(), **kwargs)
            before = await session.events(thread.thread_id)
            document = ArtifactToolResult(
                ToolResultContent(call_id=call.call_id, outcome="succeeded", output={}),
                report.document.to_jsonl(),
                copy.workspace.scope,
                report.document.summary.complete,
                publisher,
            )
            with pytest.raises(KernelError) as error:
                await publisher.publish(
                    thread.thread_id,
                    waiting.turn_id,
                    call,
                    document,
                    expected_sequence=(await session.get_thread(thread.thread_id)).sequence,
                    max_output_chars=65536,
                )
            assert error.value.code == "artifact_invalid"
            assert await session.events(thread.thread_id) == before
