"""真实 Agent v6/v7 wheel 升级探针；独立运行，不依赖测试包。"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ApprovalRequestContent,
    Budget,
    EventDraft,
    PatchApprovalRequestContent,
    ToolResultContent,
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
from harnessix.patches.agent_bridge import ManagedPatchBridge
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.managed import PatchWorkspaces
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


class ReadFixture:
    def definitions(self):
        return (
            ToolDescriptor(
                name="upgrade.read",
                version="1",
                description="旧只读审批升级",
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


def records(path):
    with sqlite3.connect(path) as db:
        return {
            row[0]: row[1]
            for row in db.execute(
                "SELECT event_id,event_json FROM agent_events ORDER BY thread_id,sequence"
            )
        }


def snapshots(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM agent_threads ORDER BY thread_id").fetchall()


def files_state(path):
    return [
        path.read_bytes().hex(),
        path.stat().st_ino,
        path.stat().st_mtime_ns,
        path.stat().st_ctime_ns,
    ]


def answer():
    return [
        ResponseStarted(response_id="answer"),
        TextStarted(content_id="answer"),
        TextCompleted(content_id="answer", text="升级验收完成"),
        ResponseCompleted(),
    ]


async def main(mode, root):
    if mode not in {"create", "upgrade", "resume", "fixture", "old-reader"}:
        raise ValueError("模式必须为 create/upgrade/resume/fixture/old-reader")
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteSessionStore(root / "session.db")
    metadata = root / "original.json"
    factory = PatchWorkspaces(root / "private")
    if mode == "old-reader":
        before = records(store.path)
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("旧 reader 意外接受新格式")
        assert before == records(store.path)
        print("真实旧 reader 拒绝新 migration，事件未改变")
        return
    if mode == "create":
        assert EventDraft.model_fields["schema_version"].default == 6
        source_path = root / "source"
        source_path.mkdir()
        target = source_path / "main.py"
        target.write_bytes(b"before\r\n")
        with Workspace(source_path) as source:
            with factory.create(source, ["main.py"], ReadOperation()) as copy:
                workspace_id = copy.workspace_id
                async with ManagedPatchBridge(copy) as bridge:
                    proposal = PatchProposal(
                        path="main.py",
                        expected_revision=read_file(
                            copy.workspace, ReadFileInput(path="main.py"), ReadOperation()
                        ).revision,
                        edits=(ExactEdit(old_text="before", new_text="after"),),
                    )
                    threads = []
                    for tool, arguments in [
                        ("upgrade.read", {}),
                        ("apply_patch", proposal.model_dump(mode="json")),
                    ]:
                        script = [
                            [
                                ResponseStarted(response_id="call"),
                                ToolCallCompleted(call_id="old", tool=tool, arguments=arguments),
                                ResponseCompleted(finish_reason="tool_calls"),
                            ]
                        ]
                        async with AgentRuntime(
                            store, ScriptedProvider(script), ReadFixture(), patches=bridge
                        ) as runtime:
                            thread = await runtime.create_thread(str(copy.workspace.root))
                            waiting = await runtime.run_turn(
                                thread.thread_id,
                                "真实旧审批",
                                request_id=tool,
                                budget=Budget(timeout_seconds=3600),
                            )
                            assert waiting.status == "waiting_approval"
                            threads.append(str(thread.thread_id))
                original = records(store.path)
                assert all(json.loads(row)["schema_version"] == 6 for row in original.values())
                metadata.write_text(
                    json.dumps(
                        {
                            "threads": threads,
                            "workspace_id": str(workspace_id),
                            "events": original,
                            "snapshots": snapshots(store.path),
                            "source": files_state(target),
                            "copy": files_state(copy.workspace.root / "main.py"),
                        },
                        ensure_ascii=False,
                    )
                )
        print("真实 v6 wheel 已创建只读和单文件两类等待审批，未写目标")
        return
    data = json.loads(metadata.read_text())
    await store.initialize()
    assert all(records(store.path)[key] == value for key, value in data["events"].items())
    if mode == "upgrade":
        assert EventDraft.model_fields["schema_version"].default == 7
        assert json.loads(json.dumps(snapshots(store.path))) == data["snapshots"]
        with factory.open(UUID(data["workspace_id"])) as copy:
            assert files_state(copy.workspace.root / "main.py") == data["copy"]
        assert files_state(root / "source" / "main.py") == data["source"]
        for thread_id in data["threads"]:
            assert replay(await store.events(UUID(thread_id))) == await store.get_thread(
                UUID(thread_id)
            )
        print("v7/migration8 升级保留旧事件与投影原字节，不消费旧批准或改文件")
        return
    expected_version = 6 if mode == "fixture" else 7
    assert EventDraft.model_fields["schema_version"].default == expected_version
    with factory.open(UUID(data["workspace_id"])) as copy:
        async with ManagedPatchBridge(copy) as bridge:
            async with AgentRuntime(
                store, ScriptedProvider([[], answer()]), ReadFixture(), patches=bridge
            ) as runtime:
                for thread_id in map(UUID, data["threads"]):
                    thread = await store.get_thread(thread_id)
                    waiting = thread.turns[-1]
                    approval = next(
                        i.content
                        for i in waiting.items
                        if isinstance(
                            i.content, ApprovalRequestContent | PatchApprovalRequestContent
                        )
                    )
                    await runtime.reply_approval(
                        thread_id,
                        waiting.turn_id,
                        approval.approval_id,
                        fingerprint=approval.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED, actor="旧包升级验收宿主"
                        ),
                    )
                    turn = await runtime.resume_turn(thread_id, waiting.turn_id)
                    assert turn.status == "completed"
                    assert replay(await store.events(thread_id)) == await store.get_thread(
                        thread_id
                    )
            assert (copy.workspace.root / "main.py").read_bytes() == b"after\r\n"
    updated = records(store.path)
    assert all(updated[key] == value for key, value in data["events"].items())
    assert all(
        json.loads(value)["schema_version"] == expected_version
        for key, value in updated.items()
        if key not in data["events"]
    )
    assert files_state(root / "source" / "main.py") == data["source"]
    if mode == "fixture":
        thread_id = UUID(data["threads"][1])
        fixture = {
            "snapshot": (await store.get_thread(thread_id)).model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in await store.events(thread_id)],
        }
        (root / "session-v6.json").write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"
        )
    print(f"v{expected_version} 实际完成旧只读/单文件审批，旧事件未改、源目录未写、Replay 一致")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
