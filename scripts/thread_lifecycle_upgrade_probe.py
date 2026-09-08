"""在独立v15与v16 wheel间验证Thread生命周期升级及旧reader拒绝。"""

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
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


def state(path: Path) -> dict[str, list[list[object]]]:
    with sqlite3.connect(path) as database:
        return {
            table: [
                [value.hex() if isinstance(value, bytes) else value for value in row]
                for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("agent_events", "agent_threads", "agent_migrations")
        }


async def create_v15(store: SQLiteSessionStore, root: Path) -> None:
    assert EventDraft.model_fields["schema_version"].default == 15
    async with AgentRuntime(store, FakeProvider("旧版本完成")) as runtime:
        thread = await runtime.create_thread(str(root))
        await runtime.run_turn(thread.thread_id, "旧版本任务", request_id="source")
    (root / "metadata.json").write_text(
        json.dumps(
            {"thread_id": str(thread.thread_id), "state": state(store.path)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("v15 wheel已创建可Fork的终结Thread")


async def main(mode: str, root: Path) -> None:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    store = SQLiteSessionStore(root / "session.sqlite")
    if mode == "create":
        await create_v15(store, root)
        return
    before = state(store.path)
    if mode == "old-reader":
        assert EventDraft.model_fields["schema_version"].default == 15
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("v15 reader错误接受migration18")
        assert state(store.path) == before
        print("v15 reader拒绝migration18且未修改数据库")
        return

    assert mode == "upgrade"
    from harnessix.agent.models import ThreadArchived, ThreadForked

    assert EventDraft.model_fields["schema_version"].default == 16
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    original = metadata["state"]
    await store.initialize()
    migrated = state(store.path)
    assert migrated["agent_events"] == original["agent_events"]
    assert migrated["agent_threads"] == original["agent_threads"]
    assert migrated["agent_migrations"][:17] == original["agent_migrations"]
    assert len(migrated["agent_migrations"]) == 18

    thread_id = UUID(metadata["thread_id"])
    provider = FakeProvider()
    async with AgentRuntime(store, provider) as runtime:
        resumed = await runtime.resume_thread(thread_id)
        child = await runtime.fork_thread(thread_id, request_id="upgrade-fork")
        archived = await runtime.archive_thread(thread_id, reason="升级验收")
    assert not provider.requests
    assert resumed.archive is None and archived.archive is not None
    assert child.fork_snapshot is not None
    assert child.fork_snapshot.source_thread_id == thread_id
    assert await store.rebuild(child.thread_id) == child
    assert await store.rebuild(thread_id) == archived
    assert replay(await store.events(child.thread_id)) == child
    assert isinstance((await store.events(child.thread_id))[0].payload, ThreadForked)
    assert isinstance((await store.events(thread_id))[-1].payload, ThreadArchived)
    after = state(store.path)
    assert after["agent_events"][: len(original["agent_events"])] == original["agent_events"]
    print("v16仅追加migration18；Resume零请求，Fork与Archive可重放且旧字节不变")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
