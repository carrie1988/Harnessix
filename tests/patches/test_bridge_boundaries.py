import asyncio
import sqlite3

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.patches import managed
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.tools.workspace import ReadOperation
from tests.patches.bridge_helpers import approval, make_call
from tests.patches.test_agent_bridge import case as case


@pytest.mark.parametrize("request_id", ["", "x" * 129, None, 1])
def test_lookup_rejects_unbounded_or_invalid_request(case, request_id):
    _, _, copy = case
    with pytest.raises(KernelError) as error:
        copy.lookup(request_id, ReadOperation())
    assert error.value.code == "patch_invalid_request"
    assert copy.lookup("x" * 128, ReadOperation()) is None


async def test_corrupt_request_index_is_not_treated_as_absent(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        with sqlite3.connect(copy.workspace.root.parent / "ledger.sqlite") as db:
            db.execute("UPDATE plans SET id='broken'")
        with pytest.raises(KernelError) as error:
            copy.lookup(plan.request_id, ReadOperation())
        assert error.value.code == "patch_ledger_corrupt"
        result = await bridge.recover(call, scope, CancelToken(), plan=plan)
        assert result.result.outcome == "unknown"


@pytest.mark.parametrize(
    "field,value", [("actor", ""), ("actor", "x" * 257), ("reason", "x" * 2001)]
)
async def test_invalid_host_decision_is_rejected_before_backend_approval(case, field, value):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = approval(plan).model_copy(update={field: value})
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, scope, plan, decision, CancelToken())
        assert error.value.code == "patch_approval_mismatch"
        assert copy.get(plan.plan_id).state == "pending"


async def test_backend_approval_conflict_does_not_write(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        copy.reply(
            plan.plan_id,
            plan.backend_fingerprint,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="其他宿主"),
        )
        with pytest.raises(KernelError) as error:
            await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert error.value.code == "patch_approval_conflict"
        assert copy.get(plan.plan_id).state == "approved"
        assert (copy.workspace.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("different_bridge", [False, True])
async def test_concurrent_execute_has_one_effect(case, different_bridge):
    source, _, copy = case
    async with ManagedPatchBridge(copy) as first, ManagedPatchBridge(copy) as second:
        call, scope = make_call(copy, first)
        plan = await first.prepare(call, scope, CancelToken())
        results = await asyncio.gather(
            first.execute(call, scope, plan, approval(plan), CancelToken()),
            (second if different_bridge else first).execute(
                call, scope, plan, approval(plan), CancelToken()
            ),
            return_exceptions=True,
        )
        assert (
            sum(
                not isinstance(r, BaseException) and r.result.outcome == "succeeded"
                for r in results
            )
            == 1
        )
        errors = [r for r in results if isinstance(r, KernelError)]
        assert len(errors) == 1 and errors[0].code in {
            "patch_not_executable",
            "patch_source_changed",
        }
        assert copy.get(plan.plan_id).state == "applied"
        assert (copy.workspace.root / "main.py").read_bytes() == b"after\r\n"
    assert (source.root / "main.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize(
    "observation", ["observed_after", "diverged", "missing", "unavailable", "uncertain"]
)
async def test_recovery_classifies_uncertain_effect_without_rewriting(
    case, monkeypatch, observation
):
    _, _, copy = case

    def interrupt(at):
        if at == "after_replace":
            raise OSError("private exception body must not escape")

    monkeypatch.setattr(managed, "_fault", interrupt)
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        result = await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert result.result.outcome == "unknown" and result.record.state == "uncertain"
        assert "private exception" not in result.result.model_dump_json()
        target = copy.workspace.root / "main.py"
        if observation == "diverged":
            target.write_bytes(b"external change")
        elif observation == "missing":
            target.unlink()
        elif observation == "unavailable":
            target.unlink()
            target.mkdir()
        elif observation == "uncertain":
            # 相同内容，不同 inode：不能归因为已批准的那次替换。
            replacement = target.with_name("other.py")
            replacement.write_bytes(target.read_bytes())
            replacement.chmod(target.stat().st_mode & 0o777)
            replacement.replace(target)
        before = target.stat() if target.exists() else None
        recovered = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        assert recovered.record.state == observation
        assert recovered.result.outcome == (
            "succeeded" if observation == "observed_after" else "unknown"
        )
        after = target.stat() if target.exists() else None
        assert (
            (before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
            if before
            else after is None
        )


async def test_unexpected_error_after_replace_requires_recovery_not_failed_result(
    case, monkeypatch
):
    _, _, copy = case

    def interrupt(at):
        if at == "after_replace":
            raise RuntimeError("注入应用层回调失败")

    monkeypatch.setattr(managed, "_fault", interrupt)
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        with pytest.raises(RuntimeError):
            await bridge.execute(call, scope, plan, approval(plan), CancelToken())
        assert copy.get(plan.plan_id).state == "uncertain"
        recovered = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        assert recovered.result.outcome == "succeeded"


async def test_lost_workspace_identity_is_unknown_not_no_effect(case):
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        root = copy.workspace.root
        root.rename(root.with_name("moved"))
        root.mkdir()
        result = await bridge.recover(
            call, scope, CancelToken(), plan=plan, approval=approval(plan)
        )
        assert result.result.outcome == "unknown"
        assert result.result.error.code == "patch_workspace_changed"


@pytest.mark.parametrize("proof", ["absent", "approved", "rejected"])
async def test_approval_without_plan_is_not_evidence_of_no_effect(case, proof):
    """恢复入口允许只带 ApprovalRecord；账本缺失时不能丢掉这份先前审批证据。"""
    _, _, copy = case
    async with ManagedPatchBridge(copy) as bridge:
        call, scope = make_call(copy, bridge)
        plan = await bridge.prepare(call, scope, CancelToken())
        decision = None if proof == "absent" else approval(plan, ApprovalOutcome(proof))
        # 模拟计划证据丢失；这不是合法执行状态，更不能被修复为可重试。
        with sqlite3.connect(copy.workspace.root.parent / "ledger.sqlite") as db:
            db.execute("DELETE FROM plans")
        result = await bridge.recover(call, scope, CancelToken(), approval=decision)
        assert result.result.outcome == ("failed" if proof == "absent" else "unknown")
        assert result.result.error.code == "patch_plan_not_found"
