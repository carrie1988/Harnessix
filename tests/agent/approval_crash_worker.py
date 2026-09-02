from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.models import ApprovalRequestContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.crash_worker import CountingTool
from tests.agent.helpers import answer, tool_step


class ApprovalCountingTool(CountingTool):
    def definitions(self):
        return tuple(
            d.model_copy(update={"requires_approval": True}) for d in super().definitions()
        )


async def main() -> None:
    database, thread, point, counter = sys.argv[1:]
    phase = ""

    def crash(name: str) -> None:
        nonlocal phase
        if name == "runtime.before_approval_request":
            phase = "request"
        elif name == "runtime.before_approval_decision":
            phase = "decision"
        cut = f"{phase}.{name.removeprefix('session.')}" if name.startswith("session.") else name
        if cut == point:
            os._exit(77)
        if name in {"runtime.after_approval_request", "runtime.after_approval_decision"}:
            phase = ""

    store = SQLiteSessionStore(database, fault=crash)
    async with AgentRuntime(
        store,
        ScriptedProvider([tool_step("test.read"), answer()]),
        ApprovalCountingTool(Path(counter)),
        fault=crash,
    ) as runtime:
        turn = await runtime.run_turn(UUID(thread), "审批进程故障测试", request_id="approval-crash")
        approval = next(
            i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
        )
        await runtime.reply_approval(
            UUID(thread),
            turn.turn_id,
            approval.approval_id,
            fingerprint=approval.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="故障测试"),
        )
        await runtime.resume_turn(UUID(thread), turn.turn_id)
    raise AssertionError("故障点未触发")


if __name__ == "__main__":
    asyncio.run(main())
