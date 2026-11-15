"""存储已满时不中断本轮（ADR 0163）：命令照常给出结果，未执行的改动明确失败。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.models import TrustedActionApprovalRequestContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.artifacts.contracts import ArtifactPolicy
from harnessix.domain.models import ApprovalOutcome
from harnessix.models.tool_result_text import tool_result_text
from tests.delivery.test_trusted_action_patch import _proposal
from tests.product_config.test_action_user_interruptions import _approved_command, _tool_result
from tests.product_config.test_patch_precondition_rejection import assert_unchanged, patch_turn
from tests.product_config.test_process_action import _TerminalRuntime
from tests.product_config.test_product_patch_rollback import product, result

FULL = ArtifactPolicy(max_live_bytes=1)


def _rows(path: Path, purpose: str) -> int:
    with sqlite3.connect(path) as db:
        query = "SELECT COUNT(*) FROM agent_artifacts WHERE purpose = ?"
        return db.execute(query, (purpose,)).fetchone()[0]


@pytest.mark.skipif(os.name != "posix", reason="固定Container Profile集成使用POSIX Owner能力")
async def test_command_result_is_kept_without_archive_when_storage_is_full(tmp_path: Path):
    # 0.10.0b46：命令已执行而存档存不下，结果记为unknown，本轮以uncertain_effect中断。
    async with _approved_command(tmp_path, _TerminalRuntime, policy=FULL) as run:
        thread_id, turn_id = run.thread.thread_id, run.approved.turn_id
        completed = await run.agent.resume_turn(thread_id, turn_id)

        assert completed.status is TurnStatus.COMPLETED, completed.error
        command = _tool_result(completed)
        assert command.outcome == "succeeded"
        assert command.trusted_action is not None
        assert command.trusted_action.state == "succeeded"
        assert isinstance(command.output, dict)
        assert command.output["artifact_omitted"] == "storage_full"
        assert "artifact" not in command.output
        assert command.output["returncode"] == 0
        assert _rows(run.sessions.path, "action_output") == 0
        assert run.process.run_calls == 1 and len(run.provider.requests) == 2
        assert replay(await run.sessions.events(thread_id)) == await run.sessions.get_thread(
            thread_id
        )


async def test_patch_is_not_applied_and_the_turn_continues_when_review_storage_is_full(
    tmp_path: Path,
):
    # 0.10.0b46：审批前的Review存档存不下，改动没有执行，本轮却以uncertain_effect中断。
    async with product(tmp_path, policy=FULL) as (root, runtime, provider, router, *_rest):
        thread, turn, call = await patch_turn(runtime, provider, root, _proposal())

        assert turn.status is TurnStatus.COMPLETED, turn.error
        output = result(turn)
        assert output.outcome == "failed"
        assert output.error is not None and output.error.code == "artifact_quota_exceeded"
        assert "was not run; nothing changed" in output.error.message
        assert not any(
            isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
        )
        assert "artifact_quota_exceeded" in tool_result_text(output.model_dump(mode="json"))
        assert len(provider.requests) == 2
        assert_unchanged(root)
        # 原Route已关闭：不能再被批准，也没有交给Executor。
        plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
        assert router.status(plan_id).state == "denied"
        decision = router.approval(plan_id).decision
        assert decision.outcome is ApprovalOutcome.REJECTED
        assert decision.reason == "artifact_quota_exceeded"
        assert all(event.executor_id is None for event in router.events(plan_id))
        assert _rows(root.parent / "state/sessions.db", "action_review") == 0
