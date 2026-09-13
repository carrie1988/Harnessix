"""真实Agent v12/v13独立wheel升级与旧前缀保护验收。"""

from __future__ import annotations

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
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


def state(path: Path) -> dict:
    with sqlite3.connect(path) as database:
        return {
            table: [
                [value.hex() if isinstance(value, bytes) else value for value in row]
                for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("agent_events", "agent_threads", "agent_artifacts", "agent_migrations")
        }


async def main(mode: str, root: Path) -> None:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    workspace = root / "workspace"
    await asyncio.to_thread(workspace.mkdir, exist_ok=True)
    store = SQLiteSessionStore(root / "session.db")
    metadata = root / "original-v12.json"
    if mode == "create":
        assert EventDraft.model_fields["schema_version"].default == 12
        (workspace / "main.py").write_text("needle 中文🙂\n" * 100)
        search = [
            ResponseStarted(response_id="search"),
            ToolCallCompleted(
                call_id="search", tool="grep", arguments={"query": "needle", "max_results": 40}
            ),
            ResponseCompleted(finish_reason="tool_calls"),
        ]
        provider = ScriptedProvider([search, FakeProvider().steps[0]])
        artifacts = SQLiteArtifactStore(store)
        async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
            async with AgentRuntime(
                store, provider, scoped_tools=tools, artifacts=artifacts
            ) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                turn = await runtime.run_turn(thread.thread_id, "搜索", request_id="v12")
        assert turn.status == "completed" and len(provider.requests) == 2
        metadata.write_text(
            json.dumps(
                {"thread_id": str(thread.thread_id), "state": state(store.path)}, ensure_ascii=False
            )
        )
        print("v12 wheel已创建真实搜索归档、完成会话和migration1-14")
        return
    before = state(store.path)
    if mode == "old-reader":
        assert EventDraft.model_fields["schema_version"].default == 12
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("v12 reader不应接受migration15")
        assert state(store.path) == before
        print("v12 reader拒绝migration15且没有改变数据库")
        return
    assert mode in {"upgrade", "resume"}
    assert EventDraft.model_fields["schema_version"].default == 20
    original = json.loads(metadata.read_text())
    await store.initialize()
    migrated = state(store.path)
    assert all(
        migrated[table] == before[table]
        for table in ("agent_events", "agent_threads", "agent_artifacts")
    )
    assert len(migrated["agent_migrations"]) == 22
    assert migrated["agent_migrations"][:14] == original["state"]["agent_migrations"]
    thread_id = UUID(original["thread_id"])
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    if mode == "upgrade":
        assert migrated["agent_threads"] == original["state"]["agent_threads"]
        print("当前v19 wheel追加migration15-22；v12事件、投影和Artifact原字节不变")
        return
    from harnessix.context.tool_result_contracts import ToolResultViewPolicy

    artifacts = SQLiteArtifactStore(store)
    provider = FakeProvider()
    async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            tool_result_view_policy=ToolResultViewPolicy(max_inline_utf8_bytes=2048),
        ) as runtime:
            blocked = await runtime.run_turn(thread_id, "缩小预算", request_id="small-policy")
            assert blocked.error.code == "context_tool_result_decision_mismatch"
            assert not provider.requests
        async with AgentRuntime(
            store, provider, scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            turn = await runtime.run_turn(thread_id, "继续", request_id="v13")
    assert turn.status == "completed" and len(provider.requests) == 1
    assert len(turn.tool_result_view_decisions) == 1
    assert turn.tool_result_view_decisions[0].strategy == "inline"
    output = next(
        i.content.output
        for i in provider.requests[0].history
        if isinstance(i.content, ToolResultContent)
    )
    assert len(output["preview"]["matches"]) == 40
    final = state(store.path)
    old = original["state"]
    assert final["agent_events"][: len(old["agent_events"])] == old["agent_events"]
    assert final["agent_artifacts"] == old["agent_artifacts"]
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    print("当前v19拒绝重裁旧模型前缀，按原视图续写检查记录；旧事件和Artifact保留")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
