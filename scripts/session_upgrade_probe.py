"""独立旧/新 wheel 执行的升级探针；不依赖源码目录或测试包。"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ApprovalRequestContent, Budget, EventDraft, ToolResultContent
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


class ReadFixture:
    def definitions(self):
        return (
            ToolDescriptor(
                name="upgrade.read",
                version="1",
                description="升级验证只读工具",
                input_schema={"type": "object"},
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=True,
                supports_reconciliation=False,
            ),
        )

    async def execute(self, call, cancel):
        cancel.checkpoint()
        return ToolResultContent(call_id=call.call_id, outcome="succeeded", output={"value": 1})


def rows(path):
    with sqlite3.connect(path) as database:
        return [
            r[0] for r in database.execute("SELECT event_json FROM agent_events ORDER BY sequence")
        ]


async def main(mode, directory):
    if mode not in {"create", "upgrade", "fixture", "old-reader"}:
        raise ValueError("模式必须为 create/upgrade/fixture/old-reader")
    store = SQLiteSessionStore(directory / "session.db")
    metadata = directory / "original.json"
    if mode == "old-reader":
        before = rows(store.path)
        try:
            async with AgentRuntime(store, ScriptedProvider([])):
                raise AssertionError("旧 reader 意外打开新库")
        except KernelError as error:
            assert error.code == "schema_too_new"
        assert rows(store.path) == before
        print("旧 reader 明确拒绝新 migration，未修改新库")
        return
    script = [
        [
            ResponseStarted(response_id="call"),
            ToolCallCompleted(call_id="read", tool="upgrade.read", arguments={}),
            ResponseCompleted(finish_reason="tool_calls"),
        ],
        [
            ResponseStarted(response_id="answer"),
            TextStarted(content_id="answer"),
            TextCompleted(content_id="answer", text="升级后完成"),
            ResponseCompleted(),
        ],
    ]
    async with AgentRuntime(store, ScriptedProvider(script), ReadFixture()) as runtime:
        if mode == "create":
            thread = await runtime.create_thread("/fixture/harnessix-v5")
            turn = await runtime.run_turn(
                thread.thread_id,
                "旧版审批",
                request_id="upgrade",
                budget=Budget(timeout_seconds=3600),
            )
            assert turn.status == "waiting_approval"
            original = rows(store.path)
            assert all(json.loads(row)["schema_version"] == 5 for row in original)
            metadata.write_text(
                json.dumps({"thread_id": str(thread.thread_id), "events": original}),
                encoding="utf-8",
            )
            print("旧 v5 wheel 已持久化真实等待审批")
            return
        data = json.loads(metadata.read_text())
        thread_id = UUID(data["thread_id"])
        thread = await store.get_thread(thread_id)
        turn = thread.turns[-1]
        approval = next(
            i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
        )
        await runtime.reply_approval(
            thread_id,
            turn.turn_id,
            approval.approval_id,
            fingerprint=approval.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="升级验收宿主"),
        )
        completed = await runtime.resume_turn(thread_id, turn.turn_id)
        assert completed.status == "completed"
        events = await store.events(thread_id)
        assert replay(events) == await store.get_thread(thread_id)
        assert rows(store.path)[: len(data["events"])] == data["events"]
        if mode == "fixture":
            assert all(e.schema_version == 5 for e in events)
            snapshot = (await store.get_thread(thread_id)).model_dump(mode="json")
            (directory / "session-v5.json").write_text(
                json.dumps(
                    {"snapshot": snapshot, "events": [e.model_dump(mode="json") for e in events]},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        else:
            assert mode == "upgrade"
            assert all(
                e.schema_version == EventDraft.model_fields["schema_version"].default
                for e in events[len(data["events"]) :]
            )
            print("新 wheel 完成旧审批；原事件字节不变、Replay 一致")


if __name__ == "__main__":
    mode, root = sys.argv[1:]
    asyncio.run(main(mode, Path(root)))
