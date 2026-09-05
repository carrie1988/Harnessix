"""真实 Agent v8 与当前 wheel 的 Session migration10/11 升级探针。"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, ToolResultContent
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
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
                description="v8升级只读工具",
                input_schema={"type": "object"},
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=False,
                supports_reconciliation=False,
            ),
        )

    async def execute(self, call, cancel):
        cancel.checkpoint()
        return ToolResultContent(call_id=call.call_id, outcome="succeeded", output={"value": 8})


def answer(response_id: str, text: str):
    return [
        ResponseStarted(response_id=response_id),
        TextStarted(content_id=response_id),
        TextCompleted(content_id=response_id, text=text),
        ResponseCompleted(),
    ]


def database_state(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as database:
        return {
            "application_id": database.execute("PRAGMA application_id").fetchone()[0],
            "migrations": database.execute(
                "SELECT version,checksum FROM agent_migrations ORDER BY version"
            ).fetchall(),
            "events": database.execute(
                "SELECT thread_id,sequence,event_id,event_json "
                "FROM agent_events ORDER BY thread_id,sequence"
            ).fetchall(),
            "threads": database.execute(
                "SELECT thread_id,sequence,snapshot_json,snapshot_sha256,projection_version "
                "FROM agent_threads ORDER BY thread_id"
            ).fetchall(),
        }


async def main(mode: str, root: Path) -> None:
    if mode not in {"create", "upgrade", "resume", "old-reader"}:
        raise ValueError("模式必须为 create/upgrade/resume/old-reader")
    await asyncio.to_thread(root.mkdir, mode=0o700, parents=True, exist_ok=True)
    store = SQLiteSessionStore(root / "session.db")
    metadata_path = root / "original-v8.json"

    if mode == "old-reader":
        inode = store.path.stat().st_ino
        before = database_state(store.path)
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("真实 v8 reader 意外接受 migration10/11")
        assert store.path.stat().st_ino == inode
        assert database_state(store.path) == before
        print("真实 v8 reader 明确拒绝 migration10/11，数据库未改变")
        return

    if mode == "create":
        assert EventDraft.model_fields["schema_version"].default == 8
        async with AgentRuntime(
            store,
            ScriptedProvider(
                [
                    [
                        ResponseStarted(response_id="v8-tool"),
                        ToolCallCompleted(call_id="v8-read", tool="upgrade.read", arguments={}),
                        ResponseCompleted(finish_reason="tool_calls"),
                    ],
                    answer("v8-answer", "真实 v8 会话"),
                ]
            ),
            ReadFixture(),
        ) as runtime:
            thread = await runtime.create_thread("/fixture/harnessix-v8")
            turn = await runtime.run_turn(
                thread.thread_id,
                "创建真实 v8 会话",
                request_id="process-session-v8",
            )
            assert turn.status == "completed"
        state = database_state(store.path)
        assert [row[0] for row in state["migrations"]] == list(range(1, 10))
        assert state["threads"][0][4] == 8
        assert all(json.loads(row[3])["schema_version"] == 8 for row in state["events"])
        metadata = {
            "thread_id": str(thread.thread_id),
            "events": state["events"],
            "threads": state["threads"],
            "migrations": state["migrations"],
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        fixture = {
            "snapshot": json.loads(state["threads"][0][2]),
            "events": [json.loads(row[3]) for row in state["events"]],
        }
        (root / "session-v8.json").write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("真实 e0e8498 v8 wheel 已创建完成会话、migration1-9和冻结夹具")
        return

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    before_inode = store.path.stat().st_ino
    before = database_state(store.path)
    assert before["events"] == [tuple(row) for row in metadata["events"]]
    assert before["threads"] == [tuple(row) for row in metadata["threads"]]
    assert before["migrations"][:9] == [tuple(row) for row in metadata["migrations"]]

    await store.initialize()
    migrated = database_state(store.path)
    assert store.path.stat().st_ino == before_inode
    assert migrated["events"] == before["events"]
    assert migrated["threads"] == before["threads"]
    assert migrated["migrations"][:9] == before["migrations"][:9]
    assert [row[0] for row in migrated["migrations"]] == list(range(1, 12))
    thread_id = UUID(metadata["thread_id"])
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)

    if mode == "upgrade":
        assert EventDraft.model_fields["schema_version"].default == 9
        print("当前wheel已原字节升级真实v8会话；migration10/11未重写事件或投影")
        return

    assert EventDraft.model_fields["schema_version"].default == 9
    old_event_count = len(before["events"])
    async with AgentRuntime(
        store, ScriptedProvider([answer("v9-answer", "migration10 后继续")])
    ) as runtime:
        completed = await runtime.run_turn(
            thread_id,
            "升级后继续",
            request_id="process-session-v9",
        )
        assert completed.status == "completed"
    resumed = database_state(store.path)
    assert resumed["events"][:old_event_count] == before["events"]
    assert all(
        json.loads(row[3])["schema_version"] == 9 for row in resumed["events"][old_event_count:]
    )
    assert resumed["threads"][0][4] == 9
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    print("v9 wheel 已在升级会话追加新事件；旧 v8 事件原字节保留且 Replay 一致")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
