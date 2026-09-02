"""离线验收：审批暂停 → 关闭宿主 → 重启答复 → 显式继续 → 确定性重放。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    ApprovalRequestContent,
    ToolCallContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore


class FixtureReader:
    def __init__(self) -> None:
        self.calls = 0

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return (
            ToolDescriptor(
                name="fixture.read",
                version="1",
                description="读取固定测试数据，不访问文件或网络",
                input_schema={"type": "object", "additionalProperties": False},
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=True,
                supports_reconciliation=False,
            ),
        )

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        self.calls += 1
        return ToolResultContent(
            call_id=call.call_id, outcome="succeeded", output={"fixture": "通过"}
        )


def provider() -> ScriptedProvider:
    return ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r1"),
                ToolCallCompleted(call_id="c1", tool="fixture.read"),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            [
                ResponseStarted(response_id="r2"),
                TextStarted(content_id="text"),
                TextCompleted(content_id="text", text="审批通过，固定数据读取完成。"),
                ResponseCompleted(finish_reason="completed"),
            ],
        ]
    )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="harnessix-approval-") as directory:
        root = Path(directory)
        store = SQLiteSessionStore(root / "session.db")
        tools = FixtureReader()
        async with AgentRuntime(store, provider(), tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "读取审批测试数据", request_id="approval"
            )
            assert turn.status == TurnStatus.WAITING_APPROVAL and tools.calls == 0
            print("审批请求已持久化；工具执行次数：0")
        async with AgentRuntime(store, provider(), tools) as runtime:
            approval = next(
                i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
            )
            await runtime.reply_approval(
                thread.thread_id,
                turn.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="离线验收"),
            )
            assert tools.calls == 0
            completed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            snapshot = await store.get_thread(thread.thread_id)
            assert completed.status == TurnStatus.COMPLETED and tools.calls == 1
            assert replay(await store.events(thread.thread_id)) == snapshot
            print("重启后审批并继续：completed；工具执行次数：1；Replay：一致；真实模型调用：0")


if __name__ == "__main__":
    asyncio.run(main())
