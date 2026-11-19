"""本轮结束时尚未开始的调用记为未执行，不再记为未知效果（Beta Issue #37遗留，负责人2026-11-18选A）。

0.10.0b55：本轮因其他原因失败时，同一步里还没有规划、仍待审批或已批准未开始的写调用一律记为
unknown，整轮以uncertain_effect中断、禁止自动重放，评测者只能人工核对命令到底有没有启动。
Router能证明它们没有开始：从未规划没有Route；待审批由系统拒绝；已批准未Claim时Executor从未调用。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.trusted_actions.contracts import UNSTARTED_PLAN_CODE
from tests.agent.test_publication import CANARY, protected
from tests.agent.test_tool_result_protection_limit import GREP, tool_calls
from tests.delivery.test_trusted_action_patch import _proposal
from tests.product_config.test_native_host_command import host_profile
from tests.product_config.test_native_process_action import _native_case, _result
from tests.product_config.test_patch_precondition_rejection import assert_unchanged
from tests.product_config.test_product_patch_rollback import product


def _results(turn: Turn) -> dict[str, ToolResultContent]:
    calls = {
        item.content.call_id: item.content.tool
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
    }
    return {
        calls[item.content.call_id]: item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
    }


async def test_a_patch_beside_a_failing_search_is_recorded_as_not_executed(
    tmp_path: Path,
) -> None:
    # 修改前：搜索结果里有受保护的值，整轮失败；同一步的改动从未规划，却记为unknown，
    # 整轮以uncertain_effect中断。
    with protected() as scope:
        async with product(tmp_path, protection=scope) as (root, runtime, provider, router, *_):
            (root / "notes.txt").write_text(f"needle {CANARY}\n", encoding="utf-8")
            proposal = _proposal()
            patch = ("patch-call", "apply_patch_batch", proposal.model_dump(mode="json"))
            provider.steps = (tool_calls(GREP, patch),)
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "搜索并修改", request_id="unstarted")

            assert turn.status is TurnStatus.FAILED
            assert turn.error is not None and turn.error.code == "public_output_secret_leak"
            results = _results(turn)
            assert results["grep"].outcome == "failed"
            not_run = results["apply_patch_batch"]
            assert not_run.outcome == "cancelled"
            assert not_run.error is not None and not_run.error.code == UNSTARTED_PLAN_CODE
            assert not any(
                isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
            )
            assert_unchanged(root)
            call = next(
                item.content
                for item in turn.items
                if isinstance(item.content, ToolCallContent)
                and item.content.call_id == not_run.call_id
            )
            plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
            with pytest.raises(KernelError) as missing:
                router.status(plan_id)
            assert missing.value.code == "action_route_not_found"
            assert CANARY not in turn.model_dump_json()
            events = await runtime.store.events(thread.thread_id)
            assert replay(events) == await runtime.store.get_thread(thread.thread_id)


async def test_an_approved_host_command_whose_turn_ends_before_it_starts_is_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 修改前：批准后、开始前本轮时限已到，Route停在ready，调用记为unknown，
    # 整轮以uncertain_effect中断。
    async with _native_case(
        tmp_path,
        "echo outside > outside.txt",
        host_profile(),
        environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    ) as case:
        await case.agent.reply_approval(
            case.thread_id,
            case.turn_id,
            case.approval.approval_id,
            fingerprint=case.approval.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
        )
        assert case.router.status(case.approval.plan_id).state == "ready"
        thread = await case.agent.store.get_thread(case.thread_id)
        created = thread.turns[-1].created_at
        monkeypatch.setattr(
            "harnessix.agent.approvals.utc_now", lambda: created + timedelta(days=1)
        )
        finished = await case.agent.resume_turn(case.thread_id, case.turn_id)

        assert finished.status is TurnStatus.FAILED
        assert finished.error is not None and finished.error.code == "time_budget_exceeded"
        assert not (case.workspace / "outside.txt").exists()
        result = _result(finished)
        assert result.outcome == "cancelled"
        assert result.error is not None and result.error.code == "time_budget_exceeded"
        assert "没有执行" in result.error.message
        route = case.router.status(case.approval.plan_id)
        assert route.state == "failed"
        events = case.router.events(case.approval.plan_id)
        assert events[-1].error_code == UNSTARTED_PLAN_CODE
        assert all(event.executor_id is None for event in events)
