"""在独立v16与v17 wheel间验证Turn Retry升级及旧reader拒绝。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseFailed
from harnessix.models.scripted import FakeProvider, ScriptedProvider
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


async def create_v16(store: SQLiteSessionStore, root: Path) -> None:
    assert EventDraft.model_fields["schema_version"].default == 16
    async with AgentRuntime(
        store, ScriptedProvider([[ResponseFailed(code="authentication")]])
    ) as runtime:
        thread = await runtime.create_thread(str(root))
        source = await runtime.run_turn(thread.thread_id, "旧版本失败任务", request_id="source")
    assert source.status is TurnStatus.FAILED
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "thread_id": str(thread.thread_id),
                "source_turn_id": str(source.turn_id),
                "state": state(store.path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("v16 wheel已创建可重试的失败Turn")


async def main(mode: str, root: Path) -> None:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    store = SQLiteSessionStore(root / "session.sqlite")
    if mode == "create":
        await create_v16(store, root)
        return
    before = state(store.path)
    if mode == "old-reader":
        assert EventDraft.model_fields["schema_version"].default == 16
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("v16 reader错误接受migration19")
        assert state(store.path) == before
        print("v16 reader拒绝migration19且未修改数据库")
        return

    assert mode == "upgrade"
    assert EventDraft.model_fields["schema_version"].default == 17
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    original = metadata["state"]
    await store.initialize()
    migrated = state(store.path)
    assert migrated["agent_events"] == original["agent_events"]
    assert migrated["agent_threads"] == original["agent_threads"]
    assert migrated["agent_migrations"][:18] == original["agent_migrations"]
    assert len(migrated["agent_migrations"]) == 19

    thread_id = UUID(metadata["thread_id"])
    source_turn_id = UUID(metadata["source_turn_id"])
    provider = FakeProvider("升级后重试完成")
    async with AgentRuntime(store, provider) as runtime:
        retried = await runtime.retry_turn(
            thread_id,
            source_turn_id,
            request_id="upgrade-retry",
        )
    assert retried.status is TurnStatus.COMPLETED
    assert retried.retry_of_turn_id == source_turn_id
    assert len(provider.requests) == 1
    current = await store.get_thread(thread_id)
    assert replay(await store.events(thread_id)) == current
    assert await store.rebuild(thread_id) == current
    after = state(store.path)
    assert after["agent_events"][: len(original["agent_events"])] == original["agent_events"]
    assert all(
        json.loads(row[3])["schema_version"] == 17
        for row in after["agent_events"][len(original["agent_events"]) :]
    )
    print("v17仅追加migration19；旧字节不变且新Retry可重放、重建")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
