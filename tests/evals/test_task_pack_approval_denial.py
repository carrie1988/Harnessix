"""固定审批范围不变：策略拒绝正式结算，不把单任务拒绝扩成Suite宿主崩溃。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemStatus, Thread, TurnStatus
from harnessix.domain.models import ApprovalOutcome, utc_now
from harnessix.evals.task_pack import builtin_coding_eval_task_pack
from harnessix.evals.task_pack_trial import _approval_decision, _drive_turn
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _approval
from tests.evals.test_grader import item
from tests.evals.test_task_pack_execution import _profile_approval
from tests.product_config.test_product_patch_rollback import product, publish, result, rollback_step


@pytest.mark.parametrize(
    "tool", ("rollback_workspace_patch", "run_profile.other", "external.write")
)
def test_unapproved_tool_is_rejected_not_promoted_to_allowed(tool):
    turn, request, case = _profile_approval({"profile": "agents-dump-compatible-refactor-check"})
    call = turn.items[0].content.model_copy(update={"tool": tool})
    turn = turn.model_copy(update={"items": (item(call),)})
    kind, decision = _approval_decision(turn, request, case)
    assert kind == "rejected" and decision.outcome is ApprovalOutcome.REJECTED
    assert decision.actor == "harnessix-eval-runner"


@pytest.mark.parametrize("selectors", (None, ["tests/custom.py"], ""))
def test_non_fixed_profile_input_remains_rejected(selectors):
    turn, request, case = _profile_approval(
        {"profile": "agents-dump-compatible-refactor-check", "selectors": selectors}
    )
    assert _approval_decision(turn, request, case)[1].outcome is ApprovalOutcome.REJECTED


def test_original_fixed_profile_decision_remains_approved():
    turn, request, case = _profile_approval({"profile": "agents-dump-compatible-refactor-check"})
    kind, decision = _approval_decision(turn, request, case)
    assert kind == "profile" and decision.outcome is ApprovalOutcome.APPROVED
    assert decision.reason == "Task Pack固定边界内的自动评测审批"


@pytest.mark.parametrize("calls", (0, 2))
def test_broken_approval_projection_still_aborts(calls):
    turn, request, case = _profile_approval({"profile": "agents-dump-compatible-refactor-check"})
    turn = turn.model_copy(update={"items": turn.items * calls})
    with pytest.raises(KernelError) as failure:
        _approval_decision(turn, request, case)
    assert failure.value.code == "eval_approval_projection_invalid"


@pytest.mark.parametrize("fault", (None, "after-rejection", "cancel-before"))
async def test_driver_uses_original_reply_boundary_for_denial(tmp_path, fault):
    """这里只测驱动控制，不把替身当Session/Owner或业务授权验收。"""
    run_id = uuid4()
    original, request, case = _profile_approval({})
    call = original.items[0].content.model_copy(update={"tool": "rollback_workspace_patch"})
    turn = original.model_copy(
        update={
            "request_id": f"coding-eval:{run_id}",
            "status": TurnStatus.WAITING_APPROVAL,
            "completed_at": None,
            "items": (item(call), item(request).model_copy(update={"status": ItemStatus.STARTED})),
        }
    )
    now = utc_now()
    thread = Thread(
        thread_id=uuid4(), workspace=str(tmp_path), created_at=now, updated_at=now, turns=(turn,)
    )
    decisions, resumes, points = [], [], []
    cancel = CancelToken()

    async def read(_control):
        return thread

    async def reply(tid, turn_id, approval_id, *, fingerprint, decision):
        assert (tid, turn_id, approval_id) == (thread.thread_id, turn.turn_id, request.approval_id)
        assert fingerprint == request.request_fingerprint
        decisions.append(decision)
        return turn.model_copy(update={"status": TurnStatus.WAITING_ACTION})

    async def resume(tid, turn_id):
        resumes.append((tid, turn_id))
        return turn.model_copy(update={"status": TurnStatus.COMPLETED, "completed_at": utc_now()})

    def checkpoint(point):
        points.append(point)
        if fault == "after-rejection":
            raise RuntimeError("simulated post-decision crash")

    if fault == "cancel-before":
        cancel.cancel()
    operation = _drive_turn(
        SimpleNamespace(reply_approval=reply, resume_turn=resume),
        SimpleNamespace(authenticated_single_thread=read),
        case,
        tmp_path,
        run_id,
        cancel,
        checkpoint,
    )
    if fault == "after-rejection":
        with pytest.raises(RuntimeError, match="post-decision crash"):
            await operation
        assert len(decisions) == 1 and not resumes
    elif fault == "cancel-before":
        with pytest.raises(TurnCancelled):
            await operation
        assert not decisions and not resumes
        return
    else:
        tid, completed = await operation
        assert tid == thread.thread_id and completed.status is TurnStatus.COMPLETED
        assert len(resumes) == 1
    assert [x.outcome for x in decisions] == [ApprovalOutcome.REJECTED]
    assert points == ["task_pack_runner.after_rejected_approval"]


async def test_fixed_eval_denial_closes_real_owned_rollback_without_effect(tmp_path):
    """真实产品Runtime/Route/事务；原修改保留，拒绝窗口恢复不执行逆向Patch。"""
    case = builtin_coding_eval_task_pack("harnessix-engineering", 2).manifest.case(
        "opencode-retry-delay-refactor"
    )
    async with product(tmp_path) as (root, runtime, provider, router, transactions, *_):
        thread_id, original_id = await publish(runtime, provider, root)
        original_record = transactions.load(original_id)
        provider.steps = (rollback_step(original_id), answer("回滚未获批准"))
        waiting = await runtime.run_turn(thread_id, "尝试撤销修改", request_id="denied-rollback")
        request = _approval(waiting)
        kind, decision = _approval_decision(waiting, request, case)
        assert kind == "rejected" and decision.outcome is ApprovalOutcome.REJECTED
        await runtime.reply_approval(
            thread_id,
            waiting.turn_id,
            request.approval_id,
            fingerprint=request.request_fingerprint,
            decision=decision,
        )
        # 已持久拒绝、尚未resume的窗口：原Route不再可批准，源事务保持原件。
        assert router.status(request.plan_id).state == "denied"
        assert all(event.executor_id is None for event in router.events(request.plan_id))
        assert router.approval(request.plan_id).decision.outcome is ApprovalOutcome.REJECTED
        completed = await runtime.resume_turn(thread_id, waiting.turn_id)
        assert completed.status is TurnStatus.COMPLETED
        assert result(completed).outcome == "failed"
        assert result(completed).error.code == "approval_rejected"
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        assert (root / "src/新增.py").is_file()
        assert not (root / "tests/deleted.txt").exists()
        assert transactions.load(original_id) == original_record
        assert await runtime.resume_turn(thread_id, waiting.turn_id) == completed
        assert transactions.load(original_id) == original_record
        assert transactions.load(request.plan_id).state == "prepared"
        assert router.status(request.plan_id).state == "denied"
