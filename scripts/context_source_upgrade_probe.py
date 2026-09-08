"""真实Agent v11与当前wheel的Session migration升级探针。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context import (
    ContextEngine,
    ContextInspectionV2,
    ContextLimits,
    ProjectInstructionSource,
    SourcedContextEngine,
)
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


def database_state(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as database:
        return {
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


def limits() -> ContextLimits:
    return ContextLimits(
        context_window_tokens=8192,
        reserved_output_tokens=1024,
        provider_overhead_tokens=0,
        safety_margin_tokens=0,
    )


async def main(mode: str, root: Path) -> None:
    if mode not in {"create", "upgrade", "resume", "old-reader"}:
        raise ValueError("模式必须为create/upgrade/resume/old-reader")
    await asyncio.to_thread(root.mkdir, mode=0o700, parents=True, exist_ok=True)
    workspace = root / "workspace"
    await asyncio.to_thread(workspace.mkdir, mode=0o700, exist_ok=True)
    database_path = root / "session.db"
    metadata_path = root / "original-v11.json"
    store = SQLiteSessionStore(database_path)

    if mode == "old-reader":
        before = database_state(database_path)
        inode = database_path.stat().st_ino
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("真实v11 reader意外接受migration15")
        assert database_path.stat().st_ino == inode
        assert database_state(database_path) == before
        print("真实v11 reader明确拒绝migration15，数据库未改变")
        return

    if mode == "create":
        assert EventDraft.model_fields["schema_version"].default == 11
        await asyncio.to_thread(
            (workspace / "AGENTS.md").write_text,
            "真实v11项目规则",
            encoding="utf-8",
        )
        planner = SourcedContextEngine(
            ContextEngine(limits()),
            (ProjectInstructionSource(workspace),),
        )
        async with AgentRuntime(store, FakeProvider(), async_context=planner) as runtime:
            thread = await runtime.create_thread(str(workspace))
            turn = await runtime.run_turn(
                thread.thread_id, "创建v11来源会话", request_id="context-source-v11"
            )
        assert len(turn.context_inspections) == 1
        assert isinstance(turn.context_inspections[0], ContextInspectionV2)
        state = database_state(database_path)
        assert [row[0] for row in state["migrations"]] == list(range(1, 14))
        assert state["threads"][0][4] == 11
        await asyncio.to_thread(
            metadata_path.write_text,
            json.dumps(
                {
                    "thread_id": str(thread.thread_id),
                    "events": state["events"],
                    "threads": state["threads"],
                    "migrations": state["migrations"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print("真实v11 wheel已创建Context Inspection v2会话与migration1-13")
        return

    metadata = json.loads(await asyncio.to_thread(metadata_path.read_text, encoding="utf-8"))
    before = database_state(database_path)
    inode = database_path.stat().st_ino
    assert before["events"] == [tuple(row) for row in metadata["events"]]
    assert before["threads"] == [tuple(row) for row in metadata["threads"]]
    await store.initialize()
    migrated = database_state(database_path)
    assert database_path.stat().st_ino == inode
    assert migrated["events"] == before["events"]
    assert migrated["threads"] == before["threads"]
    assert migrated["migrations"][:13] == [tuple(row) for row in metadata["migrations"]]
    assert [row[0] for row in migrated["migrations"]] == list(range(1, 18))
    thread_id = UUID(metadata["thread_id"])
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)

    if mode == "upgrade":
        assert EventDraft.model_fields["schema_version"].default == 15
        print("当前wheel已原字节追加migration15-17，v11事件与投影未改写")
        return

    from harnessix.context import (
        ContextInspectionV3,
        EnvironmentContextSource,
        WorkspaceContextSource,
    )

    planner = SourcedContextEngine(
        ContextEngine(limits()),
        (
            WorkspaceContextSource(workspace),
            EnvironmentContextSource(workspace, values={"CI": "true"}, allowlist=("CI",)),
        ),
    )
    old_event_count = len(before["events"])
    async with AgentRuntime(store, FakeProvider(), async_context=planner) as runtime:
        turn = await runtime.run_turn(thread_id, "追加v13来源会话", request_id="context-source-v13")
    assert len(turn.context_inspections) == 1
    assert isinstance(turn.context_inspections[0], ContextInspectionV3)
    resumed = database_state(database_path)
    assert resumed["events"][:old_event_count] == before["events"]
    assert all(
        json.loads(row[3])["schema_version"] == 15 for row in resumed["events"][old_event_count:]
    )
    assert resumed["threads"][0][4] == 15
    assert replay(await store.events(thread_id)) == await store.get_thread(thread_id)
    print(
        "v15 wheel已追加Context Inspection v3与Model History Inspection v1事件，旧v11事件原字节保留"
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
