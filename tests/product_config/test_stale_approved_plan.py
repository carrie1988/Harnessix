"""批准后、执行前工作区发生变化：操作不执行并如实反馈，本轮继续（Beta B20-02，ADR 0142）。

真实现场：审批窗口打开约两分钟，期间同一工作区根目录多了一个无关的日志文件；批准后整轮以
uncertain_effect中断，获批的新文件没有写入，用户要手工核对工作区才能继续。
"""

from __future__ import annotations

from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.trusted_actions.contracts import STALE_PLAN_CODE
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _proposal
from tests.product_config.test_product_patch_rollback import approve, product


def _patch_step(call_id: str):
    return [
        ResponseStarted(response_id=f"{call_id}-response"),
        ToolCallCompleted(
            call_id=call_id,
            tool="apply_patch_batch",
            arguments=_proposal().model_dump(mode="json"),
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def _results(turn):
    return [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]


def _approvals(turn):
    return [
        item.content
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    ]


async def test_workspace_change_while_approval_is_open_is_a_plain_failure_not_an_interruption(
    tmp_path,
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        provider.steps = (_patch_step("first"), answer("工作区刚才变了，这次没有改动"))
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改三个文件", request_id="patch")
        assert waiting.status is TurnStatus.WAITING_APPROVAL

        # 审批期间目标文件被外部改写：批准时绑定的内容已不成立。
        (root / "src/modified.py").write_bytes(b"edited elsewhere\n")
        finished = await approve(runtime, thread.thread_id, waiting)

        assert finished.status is TurnStatus.COMPLETED and finished.error is None
        (result,) = _results(finished)
        assert result.outcome == "failed"
        assert result.error is not None and result.error.code == STALE_PLAN_CODE
        assert "没有执行" in result.error.message and "重新发起" in result.error.message
        assert result.trusted_action is not None and result.trusted_action.state == "failed"
        # 没有任何写入：外部改动保留，其余目标保持原样，事务仍停在prepared。
        assert (root / "src/modified.py").read_bytes() == b"edited elsewhere\n"
        assert (root / "tests/deleted.txt").read_bytes() == b"remove\n"
        assert not (root / "src/新增.py").exists()
        plan_id = _approvals(waiting)[0].plan_id
        assert router.status(plan_id).state == "failed"
        assert transactions.load(plan_id).state == "prepared"


async def test_model_resubmits_after_a_stale_plan_and_the_second_approval_publishes(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, _, _, _):
        provider.steps = (_patch_step("first"), _patch_step("second"), answer("修改完成"))
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改三个文件", request_id="patch")
        first = _approvals(waiting)[0]

        # 根目录出现一个与本次修改无关的文件（编辑器临时文件、测试缓存、另一个终端写的日志）。
        (root / "unrelated.log").write_bytes(b"noise\n")
        again = await approve(runtime, thread.thread_id, waiting)

        # 同一轮里模型收到“未执行”的反馈并重新提交，产品为新计划重新请求审批。
        assert again.status is TurnStatus.WAITING_APPROVAL
        assert _results(again)[0].error.code == STALE_PLAN_CODE
        second = _approvals(again)[-1]
        assert second.plan_id != first.plan_id and second.decision is None
        assert router.status(first.plan_id).state == "failed"

        pending = [
            item
            for item in again.items
            if isinstance(item.content, TrustedActionApprovalRequestContent)
            and item.content.decision is None
        ]
        assert len(pending) == 1
        await runtime.reply_approval(
            thread.thread_id,
            again.turn_id,
            second.approval_id,
            fingerprint=second.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
        )
        completed = await runtime.resume_turn(thread.thread_id, again.turn_id)

        assert completed.status is TurnStatus.COMPLETED
        assert [item.outcome for item in _results(completed)] == ["failed", "succeeded"]
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        assert (root / "src/新增.py").exists() and not (root / "tests/deleted.txt").exists()
        assert (root / "unrelated.log").read_bytes() == b"noise\n"
