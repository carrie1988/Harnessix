from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.models.contracts import ModelRequest
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.context.test_compaction_runtime import accounted_text


class SummaryProvider:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    async def stream(self, request: ModelRequest, cancel: CancelToken):
        events = accounted_text("保留任务目标、public接口与恢复约束。")
        yield events[0]
        self.marker.write_text("1")
        for event in events[1:]:
            cancel.checkpoint()
            yield event


def config() -> CompactionRuntimeConfig:
    return CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=2500,
            summary_reserve_tokens=1024,
            max_summary_input_tokens=100_000,
            retain_recent_groups=1,
            min_savings_tokens=256,
        ),
        trigger_history_tokens=3000,
        max_summary_output_tokens=128,
    )


async def main() -> None:
    database, thread_id, point, marker = sys.argv[1:]

    def crash(name: str) -> None:
        if name == point:
            os._exit(77)

    store = SQLiteSessionStore(database)
    async with AgentRuntime(
        store,
        FakeProvider(),
        compaction=config(),
        summary_provider=SummaryProvider(Path(marker)),
        fault=crash,
    ) as runtime:
        await runtime.run_turn(UUID(thread_id), "继续任务", request_id="compaction-crash")
    raise AssertionError("未触发Compaction退出切点")


if __name__ == "__main__":
    asyncio.run(main())
