"""离线 Kernel 验收：执行持久 Turn，再从事件重建；不是实际模型或 Coding 演示。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore


async def main() -> None:
    with TemporaryDirectory(prefix="harnessix-kernel-") as directory:
        root = Path(directory)
        store = SQLiteSessionStore(root / "session.db")
        async with AgentRuntime(store, FakeProvider("Kernel 离线验证完成")) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证事件持久化和重放", request_id="kernel-smoke"
            )
        events = await store.events(thread.thread_id)
        snapshot = await store.get_thread(thread.thread_id)
        assert replay(events) == snapshot
        print(
            json.dumps(
                {
                    "turn_status": turn.status,
                    "event_count": len(events),
                    "replay_matches_snapshot": True,
                    "external_model_requests": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
