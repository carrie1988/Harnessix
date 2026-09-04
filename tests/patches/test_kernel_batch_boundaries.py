from dataclasses import replace
from uuid import uuid4

import pytest

from harnessix.agent import batch_patching
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    ItemStarted,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import ReadOperation
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import approval_of, batch_step, decide
from tests.patches.test_kernel_patch import results
from tests.patches.test_managed_batches import group_case as group_case
from tests.patches.test_managed_batches import snapshot


@pytest.mark.parametrize("budget", [700, 800])
async def test_result_budget_cannot_erase_applied_effect(group_case, tmp_path, budget):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        provider = ScriptedProvider([batch_step(copy, bridge, prepared)])
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(store, provider, patch_batches=bridge) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(
                thread.thread_id,
                "预算",
                request_id="budget",
                budget=Budget(max_output_chars=budget),
            )
            assert waiting.status == TurnStatus.WAITING_APPROVAL
            await decide(runtime, thread.thread_id, waiting)
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            assert turn.status == TurnStatus.FAILED and turn.error.code == "tool_output_too_large"
            assert results(turn)[0].outcome == "succeeded" and results(turn)[0].output is None
            assert results(turn)[0].patch_batch.execution.effect == "applied"
            assert results(turn)[0].patch_batch.origin == "recovery"
            assert len(provider.requests) == 1
            assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                thread.thread_id
            )


@pytest.mark.parametrize("budget", ["output", "tokens"])
async def test_budget_before_tools_never_prepares(group_case, tmp_path, monkeypatch, budget):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:

        async def forbidden(*args):
            pytest.fail("预算耗尽不能准备组计划")

        monkeypatch.setattr(bridge, "prepare", forbidden)
        step = batch_step(copy, bridge, prepared)
        if budget == "tokens":
            from harnessix.agent.models import Usage

            step[-1] = step[-1].model_copy(update={"usage": Usage(input_tokens=2, output_tokens=1)})
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), ScriptedProvider([step]), patch_batches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(
                thread.thread_id,
                "预算前置",
                request_id="pre-budget",
                budget=Budget(max_output_chars=1) if budget == "output" else Budget(max_tokens=3),
            )
            assert turn.status in {TurnStatus.FAILED, TurnStatus.INTERRUPTED}
        assert copy._db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


async def test_duplicate_or_single_port_never_grants_group_access(group_case, tmp_path):
    _, _, copy, _, prepared = group_case
    bridge = ManagedPatchBatchBridge(copy)

    class Duplicate:
        def definitions(self):
            return (bridge.definition(),)

    store = SQLiteSessionStore(tmp_path / "s.db")
    with pytest.raises(KernelError) as error:
        AgentRuntime(store, ScriptedProvider([]), Duplicate(), patch_batches=bridge)
    assert error.value.code == "duplicate_tool"
    with pytest.raises(KernelError) as error:
        AgentRuntime(store, ScriptedProvider([]), patches=bridge)
    assert error.value.code == "patch_contract_invalid"
    single = ManagedPatchBridge(copy)
    with pytest.raises(KernelError) as error:
        AgentRuntime(store, ScriptedProvider([]), patch_batches=single)
    assert error.value.code == "patch_batch_contract_invalid"
    async with bridge, single:
        async with AgentRuntime(
            store,
            ScriptedProvider([batch_step(copy, bridge, prepared)]),
            patches=single,
            patch_batches=bridge,
        ) as runtime:
            assert set(runtime._definitions) == {"apply_patch", "apply_patch_batch"}


async def test_other_workspace_never_falls_back_to_source_write(group_case, tmp_path):
    source, _, copy, _, prepared = group_case
    before = snapshot(copy.workspace.root)
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([batch_step(copy, bridge, prepared)]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(source.root))
            turn = await runtime.run_turn(thread.thread_id, "错误工作区", request_id="scope")
        assert turn.status == TurnStatus.INTERRUPTED and results(turn)[0].outcome == "unknown"
    assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("change", ["version", "missing", "fingerprint", "deadline"])
async def test_approval_or_resume_drift_does_not_execute(group_case, tmp_path, monkeypatch, change):
    _, _, copy, groups, prepared = group_case
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            store, ScriptedProvider([batch_step(copy, bridge, prepared)]), patch_batches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "失效批准", request_id="drift")
            request = approval_of(waiting)
            if change == "fingerprint":
                from tests.patches.test_kernel_patch import APPROVE

                with pytest.raises(KernelError):
                    await runtime.reply_approval(
                        thread.thread_id,
                        waiting.turn_id,
                        request.approval_id,
                        fingerprint="0" * 64,
                        decision=APPROVE,
                    )
                return
            await decide(runtime, thread.thread_id, waiting)
        provider = ScriptedProvider([])
        if change == "version":
            definition = bridge.definition().model_copy(update={"version": "changed"})
            monkeypatch.setattr(bridge, "definition", lambda: definition)
        if change == "deadline":
            monkeypatch.setattr("harnessix.agent.runtime.remaining_seconds", lambda turn: -1)
        before = snapshot(copy.workspace.root)
        async with AgentRuntime(
            store, provider, patch_batches=None if change == "missing" else bridge
        ) as runtime:
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            assert turn.status == TurnStatus.INTERRUPTED
            assert results(turn)[0].outcome == "unknown"
            assert not provider.requests
        assert groups.get_execution(request.plan.backend.batch_id, ReadOperation()) is None
        assert groups.get(request.plan.backend.batch_id, ReadOperation()).decision is None
        assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize(
    "kind", ["prepopulated", "wrong_plan", "wrong_decision", "missing_run", "no_evidence"]
)
async def test_bad_host_result_cannot_be_published_but_recovery_keeps_real_effect(
    group_case, tmp_path, monkeypatch, kind
):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        original = bridge.execute

        async def tamper(*args):
            result = await original(*args)
            if kind == "no_evidence":
                return replace(
                    result,
                    result=result.result.model_copy(update={"outcome": "failed", "output": None}),
                    plan=None,
                    approval=None,
                    execution=None,
                )
            if kind == "prepopulated":
                effect = batch_patching.PatchBatchEffect(
                    workspace_id=result.plan.backend.workspace_id,
                    batch_id=result.plan.backend.batch_id,
                    request_id=result.plan.backend.request_id,
                    approval_fingerprint=result.plan.approval_fingerprint,
                    origin="execution",
                    execution=result.execution,
                )
                return replace(
                    result, result=result.result.model_copy(update={"patch_batch": effect})
                )
            if kind == "wrong_plan":
                return replace(result, plan=result.plan.model_copy(update={"thread_id": uuid4()}))
            if kind == "wrong_decision":
                return replace(
                    result,
                    approval=result.approval.model_copy(
                        update={
                            "decision": result.approval.decision.model_copy(
                                update={"actor": "other"}
                            )
                        }
                    ),
                )
            return replace(result, execution=None)

        monkeypatch.setattr(bridge, "execute", tamper)
        provider = ScriptedProvider([batch_step(copy, bridge, prepared)])
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"), provider, patch_batches=bridge
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "伪造结果", request_id="bad-result")
            await decide(runtime, thread.thread_id, waiting)
            turn = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            assert turn.status == TurnStatus.FAILED and len(provider.requests) == 1
            assert results(turn)[0].patch_batch.origin == "recovery"
            assert results(turn)[0].patch_batch.execution.effect == "applied"


@pytest.mark.parametrize(
    "kind",
    [
        "members",
        "state",
        "effect",
        "output",
        "approval_kind",
        "request",
        "decision",
        "recovery_origin",
    ],
)
async def test_replay_cannot_forge_batch_effects_or_authorization(group_case, tmp_path, kind):
    _, _, copy, _, prepared = group_case
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            store,
            ScriptedProvider([batch_step(copy, bridge, prepared), answer()]),
            patch_batches=bridge,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "Replay边界", request_id="replay")
            await decide(runtime, thread.thread_id, waiting)
            await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    events = await store.events(thread.thread_id)
    target = next(
        e
        for e in events
        if isinstance(e.payload, ItemStarted) and isinstance(e.payload.content, ToolResultContent)
    )
    content, execution = target.payload.content, target.payload.content.patch_batch.execution
    if kind in {"approval_kind", "request", "decision"}:
        from harnessix.agent.models import ApprovalRequestContent, PatchBatchApprovalRequestContent

        target = next(
            e
            for e in events
            if isinstance(e.payload, ItemStarted)
            and isinstance(e.payload.content, PatchBatchApprovalRequestContent)
        )
        request = target.payload.content
        altered = (
            ApprovalRequestContent(
                approval_id=request.approval_id,
                call_id=request.call_id,
                request_fingerprint=request.request_fingerprint,
            )
            if kind == "approval_kind"
            else request.model_copy(update={"request_fingerprint": "0" * 64})
            if kind == "request"
            else request.model_copy(update={"decision": approve_record(request)})
        )
    else:
        if kind == "members":
            execution = execution.model_copy(update={"members": tuple(reversed(execution.members))})
        elif kind == "state":
            execution = execution.model_copy(
                update={
                    "members": (
                        execution.members[0].model_copy(update={"state": "pending"}),
                        *execution.members[1:],
                    )
                }
            )
        elif kind == "effect":
            execution = execution.model_copy(update={"effect": "partial"})
        altered = content.model_copy(
            update={
                "patch_batch": content.patch_batch.model_copy(update={"execution": execution}),
                "output": None,
            }
        )
        if kind == "output":
            altered = content.model_copy(
                update={
                    "output": {**content.output, "files": list(reversed(content.output["files"]))}
                }
            )
        if kind == "recovery_origin":
            altered = content.model_copy(
                update={
                    "patch_batch": content.patch_batch.model_copy(update={"origin": "recovery"})
                }
            )
    changed = target.model_copy(
        update={"payload": target.payload.model_copy(update={"content": altered})}
    )
    with pytest.raises(KernelError):
        replay([changed if e == target else e for e in events])


def approve_record(request):
    from harnessix.domain.models import ApprovalOutcome, ApprovalRecord

    return ApprovalRecord(
        outcome=ApprovalOutcome.APPROVED,
        request_fingerprint=request.request_fingerprint,
        actor="预置",
    )
