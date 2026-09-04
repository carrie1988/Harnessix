import json
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches import batch_agent_bridge, managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.batch_bridge_contracts import (
    ManagedPatchBatchCallPlan,
    ManagedPatchBatchOutput,
)
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batches import prepare_patch_batch
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import ReadOperation, digest
from tests.agent.helpers import answer
from tests.patches.batch_bridge_helpers import make_call
from tests.patches.bridge_helpers import approval, make_scope
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case


async def test_prepare_review_execution_reopen_and_private_evidence(group_case, monkeypatch):
    source, factory, copy, groups, prepared = group_case
    original = snapshot(source.root)
    single_definition = ManagedPatchBridge(copy).definition()
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        definition = bridge.definition()
        definition.input_schema.clear()
        assert bridge.definition().input_schema
        plan = await bridge.prepare(call, scope, CancelToken())
        assert ManagedPatchBatchCallPlan.model_validate_json(plan.model_dump_json()) == plan
        assert plan.approval_fingerprint not in {
            scope.request_fingerprint,
            plan.backend.approval_fingerprint,
        }
        assert len(plan.model_dump_json().encode()) < 65536 + 1024

        def no_prepare(*args):
            pytest.fail("已有组不得重新准备")

        monkeypatch.setattr(batch_agent_bridge, "prepare_patch_batch", no_prepare)
        assert await bridge.prepare(call, scope, CancelToken()) == plan
        assert (await bridge.review(call, scope, plan, CancelToken())).decision is None
        assert groups.get_execution(plan.backend.batch_id, ReadOperation()) is None
        result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert result.result.outcome == "succeeded" and result.execution.effect == "applied"
        output = ManagedPatchBatchOutput.model_validate_json(json.dumps(result.result.output))
        assert output.phase == "finished" and output.stop_reason == "completed"
        assert tuple(f.path for f in output.files) == PATHS
        public = result.result.model_dump_json()
        for private in (
            str(plan.thread_id),
            str(plan.turn_id),
            str(plan.backend.workspace_id),
            str(plan.backend.batch_id),
            plan.backend.request_id,
            plan.approval_fingerprint,
            plan.backend.approval_fingerprint,
            str(copy.workspace.root),
            approval(plan).actor,
            "before\\r\\n",
            "after\\r\\n",
            *(str(m.plan_id) for m in plan.backend.members),
        ):
            assert private not in public and private not in repr(result)
        assert len(public.encode()) < 4096
        with pytest.raises(KernelError, match="Patch") as error:
            await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert error.value.code == "patch_not_executable"
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, scope, CancelToken())
        assert error.value.code == "patch_not_preparable"
        with pytest.raises(KernelError) as error:
            await bridge.review(call, scope, plan, CancelToken())
        assert error.value.code == "patch_approval_closed"
        assert ManagedPatchBridge(copy).definition() == single_definition
    workspace_id = copy.workspace_id
    copy.close()
    with factory.open(workspace_id) as reopened:
        async with ManagedPatchBatchBridge(reopened) as bridge:
            recovered = await bridge.recover(
                call, scope, CancelToken(), plan=plan, approval=approval(plan)
            )
            assert recovered == result
            assert all((reopened.workspace.root / p).read_bytes() == b"after\r\n" for p in PATHS)
    assert snapshot(source.root) == original


@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("position", range(3))
async def test_stale_group_consumes_only_approved_run_and_keeps_targets(
    group_case, reject, position
):
    source, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        (copy.workspace.root / PATHS[position]).write_bytes(b"other\n")
        before = snapshot(copy.workspace.root)
        with pytest.raises(KernelError) as error:
            await bridge.review(call, scope, plan, CancelToken())
        assert error.value.code == "patch_source_changed"
        assert (
            await bridge.review(call, scope, plan, CancelToken(), verify_source=False)
        ).decision is None
        decision = approval(plan, ApprovalOutcome.REJECTED if reject else ApprovalOutcome.APPROVED)
        result = await bridge.execute(call, scope, plan, decision, CancelToken())
        assert result.result.outcome == "failed" and result.result.output["effect"] == "not_applied"
        assert (result.execution is None) == reject
        assert result.result.error.code == ("approval_rejected" if reject else "patch_not_applied")
        if not reject:
            assert result.execution.run.error_code == "patch_source_changed"
            with pytest.raises(KernelError) as error:
                await bridge.execute(call, scope, plan, decision, CancelToken())
            assert error.value.code == "patch_not_executable"
        assert snapshot(copy.workspace.root) == before
        assert all(copy.get(m.plan_id).state == "pending" for m in plan.backend.members)
        assert (
            await bridge.recover(call, scope, CancelToken(), plan=plan, approval=decision) == result
        )
    assert all((source.root / p).read_bytes() == b"before\r\n" for p in PATHS)


@pytest.mark.parametrize(
    "key", ["thread_id", "turn_id", "call_id", "workspace", "request_fingerprint"]
)
async def test_scope_mismatch(group_case, key):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        value = (
            "/wrong"
            if key == "workspace"
            else "0" * 64
            if key == "request_fingerprint"
            else uuid4()
        )
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, replace(scope, **{key: value}), CancelToken())
        assert error.value.code == "tool_scope_mismatch"


@pytest.mark.parametrize(
    "key,value",
    [
        ("tool", "apply_patch"),
        ("tool_version", "wrong"),
        ("tool_fingerprint", "0" * 64),
        ("requires_approval", False),
        ("effect_class", EffectClass.READ_ONLY),
    ],
)
async def test_tool_definition_mismatch(group_case, key, value):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, _ = make_call(copy, bridge, prepared)
        call = call.model_copy(update={key: value})
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, make_scope(copy, call), CancelToken())
        assert error.value.code == "tool_contract_changed"


@pytest.mark.parametrize(
    "field",
    [
        "thread_id",
        "turn_id",
        "call_id",
        "workspace_id",
        "batch_id",
        "plan_id",
        "scope",
        "approved",
        "actor",
        "approval_fingerprint",
    ],
)
@pytest.mark.parametrize("nested", [False, True])
async def test_model_cannot_inject_binding_fields(group_case, field, nested):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, _ = make_call(copy, bridge, prepared)
        arguments = json.loads(json.dumps(call.arguments))
        (arguments["files"][0] if nested else arguments)[field] = "injected"
        call = call.model_copy(update={"arguments": arguments})
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, make_scope(copy, call), CancelToken())
        assert error.value.code == "tool_invalid_arguments"


@pytest.mark.parametrize(
    "key",
    ["thread_id", "turn_id", "call_id", "call_fingerprint", "approval_fingerprint", "backend"],
)
async def test_tampered_complete_plan_never_authorizes(group_case, key):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        value = uuid4() if key.endswith("_id") else "0" * 64
        if key == "backend":
            value = plan.backend.model_copy(
                update={"members": tuple(reversed(plan.backend.members))}
            )
        tampered = plan.model_copy(update={key: value})
        with pytest.raises(ValidationError):
            ManagedPatchBatchCallPlan.model_validate_json(tampered.model_dump_json())
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, scope, tampered, approval(tampered), CancelToken())
        assert error.value.code == "patch_call_mismatch"
        assert groups.get(plan.backend.batch_id, ReadOperation()).decision is None


@pytest.mark.parametrize("which", ["call", "backend", "member", "random"])
async def test_other_approval_scope_cannot_authorize(group_case, which):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        wrong = {
            "call": scope.request_fingerprint,
            "backend": plan.backend.approval_fingerprint,
            "member": plan.backend.members[0].approval_fingerprint,
            "random": "0" * 64,
        }[which]
        with pytest.raises(KernelError) as error:
            await bridge.execute(
                call,
                scope,
                plan,
                approval(plan).model_copy(update={"request_fingerprint": wrong}),
                CancelToken(),
            )
        assert error.value.code == "patch_approval_mismatch"
        assert groups.get(plan.backend.batch_id, ReadOperation()).decision is None


async def test_other_call_and_copy_cannot_reuse_plan(group_case):
    source, factory, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, make_scope(copy, call), plan, approval(plan), CancelToken())
        assert error.value.code == "patch_plan_not_found"
        with factory.create(source, PATHS, ReadOperation()) as other:
            async with ManagedPatchBatchBridge(other) as other_bridge:
                assert bridge.definition().version != other_bridge.definition().version
                with pytest.raises(KernelError) as error:
                    await other_bridge.prepare(call, scope, CancelToken())
                assert error.value.code == "patch_workspace_mismatch"


@pytest.mark.parametrize("change", ["text", "order"])
async def test_original_request_cannot_be_bound_to_other_proposal(group_case, change):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        arguments = json.loads(json.dumps(call.arguments))
        if change == "text":
            arguments["files"][0]["edits"][0]["new_text"] = "else"
        else:
            arguments["files"].reverse()
        wrong = prepare_patch_batch(
            copy.workspace,
            PatchBatchProposal.model_validate_json(json.dumps(arguments)),
            ReadOperation(),
        )
        groups.save(wrong, bridge._request(scope), ReadOperation())
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, scope, CancelToken())
        assert error.value.code == "patch_call_mismatch"


@pytest.mark.parametrize("state", ["pending", "approved", "rejected", "applied"])
@pytest.mark.parametrize(
    "proof", ["valid", "no_plan", "no_approval", "wrong_actor", "wrong_fingerprint"]
)
async def test_recovery_requires_complete_original_approval(group_case, monkeypatch, state, proof):
    _, _, copy, groups, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(
            plan, ApprovalOutcome.REJECTED if state == "rejected" else ApprovalOutcome.APPROVED
        )
        if state != "pending":
            groups.reply(
                plan.backend.batch_id,
                plan.backend.approval_fingerprint,
                ApprovalDecision.model_validate_json(
                    decision.model_dump_json(include={"actor", "outcome", "reason"})
                ),
                ReadOperation(),
            )
        if state == "applied":
            groups.execute(
                plan.backend.batch_id, plan.backend.approval_fingerprint, ReadOperation()
            )
        before = snapshot(copy.workspace.root)

        def forbidden(*args):
            pytest.fail("恢复不得准备、保存、批准、执行或复核当前提案")

        monkeypatch.setattr(batch_agent_bridge, "prepare_patch_batch", forbidden)
        for method in ("save", "reply", "execute", "verify"):
            monkeypatch.setattr(ManagedPatchBatches, method, forbidden)
        supplied = (
            None
            if proof == "no_approval"
            else decision.model_copy(update={"actor": "其他"})
            if proof == "wrong_actor"
            else decision.model_copy(update={"request_fingerprint": "0" * 64})
            if proof == "wrong_fingerprint"
            else decision
        )
        result = await bridge.recover(
            call, scope, CancelToken(), plan=None if proof == "no_plan" else plan, approval=supplied
        )
        assert result.result.outcome == (
            "succeeded"
            if proof == "valid" and state == "applied"
            else "failed"
            if proof == "valid" and state in {"approved", "rejected"}
            else "unknown"
        )
        assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("damage", ["missing", "manifest", "run"])
async def test_missing_or_damaged_persistent_facts_are_unknown(group_case, damage):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        missing = await bridge.recover(call, scope, CancelToken())
        assert missing.result.outcome == "unknown"
        plan = await bridge.prepare(call, scope, CancelToken())
        await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        before = snapshot(copy.workspace.root)
        with copy._db:
            if damage == "missing":
                copy._db.execute("UPDATE batches SET request_id='other'")
            elif damage == "manifest":
                copy._db.execute("UPDATE batches SET checksum='corrupt'")
            else:
                copy._db.execute("DELETE FROM batch_run_events WHERE phase='started'")
        recovered = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        assert recovered.result.outcome == "unknown" and recovered.result.output is None
        assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
async def test_partial_and_unknown_projection(group_case, monkeypatch, position, point):
    _, _, copy, _, prepared = group_case
    count = 0

    def fail_at(at):
        nonlocal count
        if at == point:
            index, count = count, count + 1
            if index == position:
                raise OSError("故障注入")

    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        monkeypatch.setattr(managed, "_fault", fail_at)
        result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        expected = (
            "unknown" if point == "after_replace" else "partial" if position else "not_applied"
        )
        assert result.result.output["effect"] == expected
        assert result.result.outcome == ("unknown" if point == "after_replace" else "failed")
        assert all(m.state == "pending" for m in result.execution.members[position + 1 :])
        monkeypatch.setattr(managed, "_fault", lambda _: None)
        before = snapshot(copy.workspace.root)
        recovered = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        applied = position + int(point == "after_replace")
        assert recovered.result.output["effect"] == (
            "applied" if applied == 3 else "partial" if applied else "not_applied"
        )
        assert recovered.result.output["stop_reason"] == "failed"
        assert snapshot(copy.workspace.root) == before


async def test_default_kernel_denies_advertised_batch_definition(group_case, tmp_path):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        assert not hasattr(bridge, "definitions") and not hasattr(bridge, "execute_scoped")
        call, _ = make_call(copy, bridge, prepared)

        class AccidentalRegistration:
            def definitions(self):
                return (bridge.definition(),)

            async def execute(self, *args):
                pytest.fail("Kernel 尚未接入批量写工具")

        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="call"),
                    ToolCallCompleted(call_id="batch", tool=call.tool, arguments=call.arguments),
                    ResponseCompleted(finish_reason="tool_calls"),
                ],
                answer(),
            ]
        )
        before = snapshot(copy.workspace.root)
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"), provider, AccidentalRegistration()
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(
                thread.thread_id, "拒绝未接入的批量写工具", request_id="no-batch"
            )
        assert turn.status == TurnStatus.COMPLETED
        results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
        assert results[0].error.code == "tool_not_enabled"
        assert all(not r.tools for r in provider.requests)
        assert snapshot(copy.workspace.root) == before


@pytest.mark.parametrize("field", ["thread_id", "turn_id", "call_id", "call_fingerprint"])
async def test_rehashing_outer_plan_does_not_fix_request_binding(group_case, field):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        data = plan.model_dump(mode="json", exclude={"approval_fingerprint"})
        data[field] = str(uuid4()) if field.endswith("_id") else "0" * 64
        data["approval_fingerprint"] = digest(data)
        with pytest.raises(ValidationError):
            ManagedPatchBatchCallPlan.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "field", ["batch_id", "workspace_id", "approval_fingerprint", "members", "call_id"]
)
async def test_result_wrong_group_or_member_order_rejected(group_case, field):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        execution = result.execution
        if field == "call_id":
            call = call.model_copy(update={field: uuid4()})
        elif field == "members":
            execution = execution.model_copy(update={field: tuple(reversed(execution.members))})
        else:
            value = "0" * 64 if field == "approval_fingerprint" else uuid4()
            execution = execution.model_copy(
                update={"run": execution.run.model_copy(update={field: value})}
            )
        with pytest.raises(KernelError) as error:
            bridge._result(call, plan, result.approval, execution)
        assert error.value.code == "patch_result_mismatch"


@pytest.mark.parametrize(
    "damage", ["effect", "phase", "reason", "order", "member_effect", "duplicate"]
)
async def test_public_output_cannot_lie_about_effects(group_case, damage):
    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        data = result.result.output
        if damage == "effect":
            data["effect"] = "not_applied"
        elif damage == "phase":
            data["phase"] = "not_started"
        elif damage == "reason":
            data["stop_reason"] = None
        elif damage == "order":
            data["files"][0].update(state="pending", effect="not_applied")
        elif damage == "member_effect":
            data["files"][0]["effect"] = "unknown"
        else:
            data["files"][1]["path"] = data["files"][0]["path"]
        with pytest.raises(ValidationError):
            ManagedPatchBatchOutput.model_validate_json(json.dumps(data))


async def test_concurrent_execution_consumes_once(group_case):
    import asyncio

    _, _, copy, _, prepared = group_case
    async with ManagedPatchBatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge, prepared)
        plan = await bridge.prepare(call, scope, CancelToken())
        results = await asyncio.gather(
            *(bridge.execute(call, scope, plan, approval(plan), CancelToken()) for _ in range(2)),
            return_exceptions=True,
        )
        assert (
            sum(isinstance(r, KernelError) and r.code == "patch_not_executable" for r in results)
            == 1
        )
        assert (
            sum(not isinstance(r, Exception) and r.result.outcome == "succeeded" for r in results)
            == 1
        )
        assert copy._db.execute("SELECT COUNT(*) FROM batch_run_events").fetchone()[0] == 2


def test_maximum_escaped_paths_remain_within_public_budget():
    from harnessix.patches.batch_bridge_contracts import MAX_BATCH_OUTPUT_BYTES, BatchFileOutput

    files = tuple(
        BatchFileOutput(
            path="/".join(['"' * 250] * 4) + f"{i}.py",
            state="applied",
            effect="applied",
            before_sha256="0" * 64,
            after_sha256="1" * 64,
        )
        for i in range(16)
    )
    result = ManagedPatchBatchOutput(
        phase="finished", stop_reason="completed", effect="applied", files=files
    )
    assert len(result.model_dump_json().encode()) < MAX_BATCH_OUTPUT_BYTES


async def test_maximum_sixteen_file_call_executes_and_reopens(group_case):
    from tests.patches.test_managed_batches import prepare

    source, factory, _, _, _ = group_case
    paths = tuple(f"file-{i}.py" for i in range(16))
    for path in paths:
        (source.root / path).write_bytes(b"before\r\n")
    with factory.create(source, paths, ReadOperation()) as copy:
        workspace_id = copy.workspace_id
        async with ManagedPatchBatchBridge(copy) as bridge:
            call, scope = make_call(copy, bridge, prepare(copy, paths))
            plan = await bridge.prepare(call, scope, CancelToken())
            result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
            assert result.execution.effect == "applied"
            assert len(result.result.output["files"]) == 16
    with factory.open(workspace_id) as reopened:
        async with ManagedPatchBatchBridge(reopened) as bridge:
            assert (
                await bridge.recover(call, scope, CancelToken(), plan=plan, approval=approval(plan))
                == result
            )
    assert all((source.root / path).read_bytes() == b"before\r\n" for path in paths)
