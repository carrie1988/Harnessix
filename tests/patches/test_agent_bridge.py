import json
import sqlite3
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
from harnessix.patches import agent_bridge
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.bridge_contracts import ManagedPatchCallPlan
from harnessix.patches.managed import PatchWorkspaces
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.workspace import ReadOperation, Workspace, digest
from tests.agent.helpers import answer
from tests.patches.bridge_helpers import approval, make_call, make_scope


@pytest.fixture
def case(tmp_path):
    source_path = tmp_path / "source"
    source_path.mkdir()
    (source_path / "main.py").write_bytes(b"before\r\n")
    factory = PatchWorkspaces(tmp_path / "private")
    with Workspace(source_path) as source:
        with factory.create(source, ["main.py"], ReadOperation()) as copy:
            yield source, factory, copy


async def test_prepare_review_approve_execute_reopen(case, monkeypatch):
    source, factory, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        assert plan == ManagedPatchCallPlan.model_validate_json(plan.model_dump_json())
        assert plan.approval_fingerprint not in {
            scope.request_fingerprint,
            plan.backend_fingerprint,
        }
        assert copy.lookup(plan.request_id, ReadOperation()).state == "pending"
        assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"

        def no_prepare(*args):
            pytest.fail("已有计划不能重新准备")

        monkeypatch.setattr(agent_bridge, "prepare_patch", no_prepare)
        assert await bridge.prepare(call, scope, CancelToken()) == plan
        assert (await bridge.review(call, scope, plan, CancelToken())).state == "pending"
        result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert result.record.state == "applied" and result.result.outcome == "succeeded"
        assert result.plan == plan
        public = result.result.model_dump_json()
        for private in (
            str(plan.workspace_id),
            str(plan.plan_id),
            str(plan.thread_id),
            str(plan.turn_id),
            plan.backend_fingerprint,
            plan.approval_fingerprint,
            plan.call_fingerprint,
            str(copy.workspace.root),
            "before\\r\\n",
            "after\\r\\n",
        ):
            assert private not in public
        assert len(public) < 1500
        with pytest.raises(KernelError, match="Patch") as error:
            await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert error.value.code == "patch_not_executable"
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, scope, CancelToken())
        assert error.value.code == "patch_not_preparable"
        definition = bridge.definition()
    # 关闭桥接不关闭宿主副本，反之宿主必须等桥接操作退出后再关闭。
    assert copy.get(plan.plan_id).state == "applied"
    copy.close()
    with factory.open(plan.workspace_id) as reopened:
        async with ManagedPatchBridge(reopened) as bridge:
            assert bridge.definition() == definition
            recovered = await bridge.recover(
                call, scope, CancelToken(), plan=plan, approval=approval(plan)
            )
            assert recovered == result
            assert (reopened.workspace.root / "main.py").read_bytes() == b"after\r\n"
    assert (source.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("reject", [False, True])
async def test_stale_plan_does_not_replace_or_approve(case, reject):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        target = copy.workspace.root / "main.py"
        target.write_bytes(b"changed\n")
        for operation in (
            bridge.prepare(call, scope, CancelToken()),
            bridge.review(call, scope, plan, CancelToken()),
        ):
            with pytest.raises(KernelError) as error:
                await operation
            assert error.value.code == "patch_source_changed"
        decision = approval(plan, ApprovalOutcome.REJECTED if reject else ApprovalOutcome.APPROVED)
        if reject:
            result = await bridge.execute(call, scope, plan, decision, CancelToken())
            assert result.record.state == "rejected"
            assert result.result.error.code == "approval_rejected"
        else:
            with pytest.raises(KernelError) as error:
                await bridge.execute(call, scope, plan, decision, CancelToken())
            assert error.value.code == "patch_source_changed"
            assert copy.get(plan.plan_id).state == "pending"
        assert target.read_bytes() == b"changed\n"


@pytest.mark.parametrize(
    "key", ["thread_id", "turn_id", "call_id", "workspace", "request_fingerprint"]
)
async def test_wrong_execution_scope_rejected(case, key):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        value = (
            "/different"
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
        ("tool", "other"),
        ("tool_version", "changed"),
        ("tool_fingerprint", "0" * 64),
        ("requires_approval", False),
        ("effect_class", EffectClass.READ_ONLY),
    ],
)
async def test_host_scope_does_not_override_tool_contract(case, key, value):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        original, _ = make_call(copy, bridge)
        call = original.model_copy(update={key: value})
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, make_scope(copy, call), CancelToken())
        assert error.value.code == "tool_contract_changed"


@pytest.mark.parametrize(
    "key",
    [
        "thread_id",
        "turn_id",
        "call_id",
        "workspace_id",
        "plan_id",
        "request_id",
        "approval_fingerprint",
        "actor",
        "approved",
        "scope",
    ],
)
async def test_model_cannot_inject_host_fields(case, key):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        original, _ = make_call(copy, bridge)
        call = original.model_copy(update={"arguments": {**original.arguments, key: "injected"}})
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, make_scope(copy, call), CancelToken())
        assert error.value.code == "tool_invalid_arguments"


@pytest.mark.parametrize(
    "field",
    [
        "thread_id",
        "turn_id",
        "call_id",
        "workspace_id",
        "plan_id",
        "request_id",
        "call_fingerprint",
        "backend_fingerprint",
        "approval_fingerprint",
    ],
)
async def test_tampered_plan_cannot_consume_approval(case, field):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        value = uuid4() if field.endswith("_id") and field != "request_id" else "0" * 64
        tampered = plan.model_copy(update={field: value})
        with pytest.raises(ValidationError):
            ManagedPatchCallPlan.model_validate_json(tampered.model_dump_json())
        # 即使绕过 Pydantic 建模并让宿主签了篡改指纹，也必须匹配磁盘原计划。
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, scope, tampered, approval(tampered), CancelToken())
        assert error.value.code == "patch_call_mismatch"
        assert copy.get(plan.plan_id).state == "pending"


@pytest.mark.parametrize("fingerprint", ["read", "backend", "random"])
async def test_read_or_backend_approval_cannot_authorize_bridge(case, fingerprint):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        wrong = {
            "read": scope.request_fingerprint,
            "backend": plan.backend_fingerprint,
            "random": "0" * 64,
        }[fingerprint]
        decision = approval(plan).model_copy(update={"request_fingerprint": wrong})
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, scope, plan, decision, CancelToken())
        assert error.value.code == "patch_approval_mismatch"
        assert copy.get(plan.plan_id).state == "pending"


async def test_copied_plan_cannot_be_used_for_other_call_or_copy(case):
    source, factory, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        other_scope = make_scope(copy, call)
        assert other_scope.request_fingerprint != scope.request_fingerprint
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, other_scope, plan, approval(plan), CancelToken())
        assert error.value.code == "patch_plan_not_found"
        with factory.create(source, ["main.py"], ReadOperation()) as other:
            async with ManagedPatchBridge(other) as other_bridge:
                assert other_bridge.definition().version != bridge.definition().version
                with pytest.raises(KernelError) as error:
                    await other_bridge.prepare(call, scope, CancelToken())
                assert error.value.code == "patch_workspace_mismatch"
        assert copy.get(plan.plan_id).state == "pending"


async def test_record_under_same_request_must_match_original_proposal(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        changed = call.model_copy(
            update={
                "arguments": {
                    **call.arguments,
                    "edits": [{"old_text": "before", "new_text": "other"}],
                }
            }
        )
        from harnessix.patches.contracts import PatchProposal
        from harnessix.patches.planner import prepare_patch

        proposal = PatchProposal.model_validate_json(json.dumps(changed.arguments))
        prepared = prepare_patch(copy.workspace, proposal, ReadOperation())
        copy.save(prepared, bridge._request(scope), ReadOperation())
        with pytest.raises(KernelError) as error:
            await bridge.prepare(call, scope, CancelToken())
        assert error.value.code == "patch_call_mismatch"


async def test_recovery_never_creates_or_approves_or_executes(case, monkeypatch):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        absent = await bridge.recover(call, scope, CancelToken())
        assert absent.result.outcome == "failed" and absent.plan is None
        plan = await bridge.prepare(call, scope, CancelToken())

        def forbidden(*args):
            pytest.fail("恢复不得准备、批准或执行")

        monkeypatch.setattr(agent_bridge, "prepare_patch", forbidden)
        for method in ("save", "reply", "execute", "verify"):
            monkeypatch.setattr(copy, method, forbidden)
        for provided in (None, plan):
            result = await bridge.recover(call, scope, CancelToken(), plan=provided)
            assert result.result.outcome == "failed" and result.record.state == "pending"
            assert result.plan == plan
        assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("state", ["approved", "applied"])
@pytest.mark.parametrize("proof", ["none", "valid", "wrong_fingerprint", "wrong_actor"])
async def test_recovery_requires_host_approval_before_reporting_success(case, state, proof):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan)
        copy.reply(
            plan.plan_id,
            plan.backend_fingerprint,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor=decision.actor),
        )
        if state == "applied":
            copy.execute(plan.plan_id, plan.backend_fingerprint, ReadOperation())
        supplied = (
            None
            if proof == "none"
            else decision.model_copy(update={"request_fingerprint": "0" * 64})
            if proof == "wrong_fingerprint"
            else decision.model_copy(update={"actor": "其他"})
            if proof == "wrong_actor"
            else decision
        )
        result = await bridge.recover(call, scope, CancelToken(), plan=plan, approval=supplied)
        expected = (
            "unknown"
            if proof == "wrong_fingerprint"
            else "failed"
            if state == "approved"
            else "succeeded"
            if proof == "valid"
            else "unknown"
        )
        assert result.result.outcome == expected


async def test_known_plan_missing_or_corrupt_is_unknown(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        with sqlite3.connect(copy.workspace.root.parent / "ledger.sqlite") as db:
            db.execute("UPDATE plans SET before_image=?", (b"corrupt",))
        result = await bridge.recover(call, scope, CancelToken(), plan=plan)
        assert result.result.outcome == "unknown"
        assert result.result.error.code == "patch_plan_corrupt"
        with sqlite3.connect(copy.workspace.root.parent / "ledger.sqlite") as db:
            db.execute("DELETE FROM plans")
        result = await bridge.recover(call, scope, CancelToken(), plan=plan)
        assert result.result.outcome == "unknown"
        assert result.result.error.code == "patch_plan_not_found"


async def test_default_kernel_still_denies_even_explicitly_advertised_bridge_definition(
    case, tmp_path
):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        assert not hasattr(bridge, "definitions") and not hasattr(bridge, "execute_scoped")
        call, _ = make_call(copy, bridge)

        class AccidentalRegistration:
            def definitions(self):
                return (bridge.definition(),)

            async def execute(self, *args):
                pytest.fail("旧 Kernel 不得执行写桥接")

        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="call"),
                    ToolCallCompleted(call_id="write", tool=call.tool, arguments=call.arguments),
                    ResponseCompleted(finish_reason="tool_calls"),
                ],
                answer(),
            ]
        )
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"), provider, AccidentalRegistration()
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            turn = await runtime.run_turn(
                thread.thread_id, "拒绝尚未开放的工具", request_id="no-write"
            )
        assert turn.status == TurnStatus.COMPLETED
        results = [
            item.content for item in turn.items if isinstance(item.content, ToolResultContent)
        ]
        assert results[0].outcome == "failed" and results[0].error.code == "tool_not_enabled"
        assert all(not request.tools for request in provider.requests)
        assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


async def test_schema_detects_recomputed_outer_hash_over_wrong_request(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        data = plan.model_dump(mode="json", exclude={"approval_fingerprint"})
        data["request_id"] = "0" * 64
        data["approval_fingerprint"] = digest(data)
        with pytest.raises(ValidationError):
            ManagedPatchCallPlan.model_validate_json(json.dumps(data))
