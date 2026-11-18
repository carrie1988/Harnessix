"""真实产品装配下，保护检查做不完只影响那一次调用（ADR 0174，Beta Issue #37）。

0.10.0b55：同一步里搜索的完整存档检查超限时，整轮失败，旁边还没审批的测试命令被记为未知效果、
以uncertain_effect中断；命令已执行而完整输出检查超限、或改动的Review检查超限时整轮同样失败。
"""

from __future__ import annotations

import os
import sqlite3
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.approvals import trusted_action_invocation_id
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.domain.models import ApprovalOutcome
from harnessix.models.tool_result_text import tool_result_text
from harnessix.secrets import publication
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from tests.agent.test_publication import CANARY, REFERENCE, protected
from tests.agent.test_tool_result_protection_limit import ARCHIVE_ONLY_LIMIT, GREP, tool_calls
from tests.delivery.test_trusted_action_patch import _proposal
from tests.product_config.test_action_user_interruptions import _RUN_TESTS, _approved_command
from tests.product_config.test_patch_precondition_rejection import assert_unchanged, patch_turn
from tests.product_config.test_process_action import _TerminalRuntime
from tests.product_config.test_product_patch_rollback import product, result

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="固定Container Profile集成使用POSIX Owner能力"
)
RUN_TESTS = next(event for event in _RUN_TESTS if hasattr(event, "tool"))


def _results(turn: Turn) -> list[ToolResultContent]:
    return [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]


def _rows(path: Path, purpose: str) -> int:
    with sqlite3.connect(path) as db:
        query = "SELECT COUNT(*) FROM agent_artifacts WHERE purpose = ?"
        return db.execute(query, (purpose,)).fetchone()[0]


@posix_only
async def test_a_search_beside_a_command_no_longer_marks_the_command_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 修改前：进入不了审批，命令记为unknown，本轮以uncertain_effect中断（报告里的Fastify会话）。
    monkeypatch.setattr(publication, "MAX_SCAN_WORK", ARCHIVE_ONLY_LIMIT)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("needle 中文\n" * 300, encoding="utf-8")
    command = (RUN_TESTS.call_id, RUN_TESTS.tool, RUN_TESTS.arguments)
    with protected() as scope:
        step = tool_calls(GREP, command)
        async with _approved_command(
            tmp_path, _TerminalRuntime, protection=scope, first_step=step
        ) as run:
            (search,) = _results(run.approved)
            assert search.outcome == "succeeded"
            assert isinstance(search.output, dict)
            assert search.output["artifact_omitted"] == "protection_limit"
            completed = await run.agent.resume_turn(run.thread.thread_id, run.approved.turn_id)

            assert completed.status is TurnStatus.COMPLETED, completed.error
            _, executed = _results(completed)
            assert executed.outcome == "succeeded"
            assert executed.trusted_action is not None
            assert executed.trusted_action.state == "succeeded"
            assert run.process.run_calls == 1 and len(run.provider.requests) == 2
            assert _rows(run.sessions.path, "tool_result") == 0
            events = await run.sessions.events(run.thread.thread_id)
            assert replay(events) == await run.sessions.get_thread(run.thread.thread_id)


@posix_only
async def test_command_output_too_large_to_check_keeps_the_result_without_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 修改前：命令已经执行，整轮以public_output_limit失败。
    monkeypatch.setattr(publication, "MAX_SCAN_WORK", ARCHIVE_ONLY_LIMIT)
    process = partial(_TerminalRuntime, stdout=b"collected 1 item\n" * 12_000)
    with protected() as scope:
        async with _approved_command(tmp_path, process, protection=scope) as run:
            completed = await run.agent.resume_turn(run.thread.thread_id, run.approved.turn_id)

            assert completed.status is TurnStatus.COMPLETED, completed.error
            (executed,) = _results(completed)
            assert executed.outcome == "succeeded"
            assert executed.trusted_action is not None
            assert executed.trusted_action.state == "succeeded"
            assert isinstance(executed.output, dict)
            assert executed.output["artifact_omitted"] == "protection_limit"
            assert "artifact" not in executed.output
            assert executed.output["returncode"] == 0
            text = tool_result_text(executed.model_dump(mode="json", include={"outcome", "output"}))
            assert "full output not saved (too large to check for protected values)" in text
            assert _rows(run.sessions.path, "action_output") == 0
            assert run.process.run_calls == 1 and len(run.provider.requests) == 2


class _ReviewTooLarge(SecretPublicationScope):
    """真实作用域；改动Review存档的检查报告做不完，其他检查照常。"""

    def __init__(self) -> None:
        provider = EnvironmentSecretProvider(
            (EnvironmentSecretSource("api", "9", "MODEL_KEY"),), environment={"MODEL_KEY": CANARY}
        )
        super().__init__((REFERENCE,), provider)

    def assert_public_jsonl(self, body: bytes, *, checkpoint: Any) -> None:
        if b"harnessix.workspace-action-review/" in body:
            raise KernelError("trusted_action_output_limit", "检查没有做完")
        super().assert_public_jsonl(body, checkpoint=checkpoint)


async def test_patch_review_too_large_to_check_is_not_run_and_the_turn_continues(
    tmp_path: Path,
) -> None:
    # 修改前：改动没有执行，原Route停在待审批，本轮以uncertain_effect中断。
    with _ReviewTooLarge() as scope:
        async with product(tmp_path, protection=scope) as (root, runtime, provider, router, *_):
            thread, turn, call = await patch_turn(runtime, provider, root, _proposal())

            assert turn.status is TurnStatus.COMPLETED, turn.error
            output = result(turn)
            assert output.outcome == "failed"
            assert output.error is not None and output.error.code == "public_output_limit"
            assert "was not run; nothing changed" in output.error.message
            assert not any(
                isinstance(item.content, TrustedActionApprovalRequestContent) for item in turn.items
            )
            assert len(provider.requests) == 2
            assert_unchanged(root)
            plan_id = trusted_action_invocation_id(thread.thread_id, turn.turn_id, call)
            assert router.status(plan_id).state == "denied"
            decision = router.approval(plan_id).decision
            assert decision.outcome is ApprovalOutcome.REJECTED
            assert decision.reason == "public_output_limit"
            assert all(event.executor_id is None for event in router.events(plan_id))
            assert _rows(root.parent / "state/sessions.db", "action_review") == 0
