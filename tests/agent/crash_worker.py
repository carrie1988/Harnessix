from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer, tool_step


class CountingTool:
    def __init__(self, path: Path) -> None:
        self.path = path

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return (
            ToolDescriptor(
                name="test.read",
                version="1",
                description="进程故障夹具",
                input_schema={"type": "object"},
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=False,
                supports_reconciliation=False,
            ),
        )

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        count = int(self.path.read_text()) if self.path.exists() else 0
        self.path.write_text(str(count + 1))
        return ToolResultContent(call_id=call.call_id, outcome="succeeded", output=count + 1)


async def main() -> None:
    database, thread, point, counter = sys.argv[1:]
    store = SQLiteSessionStore(database, fault=crash if point.startswith("session.") else None)
    runtime_fault = crash if point.startswith("runtime.") else None
    provider = ScriptedProvider([tool_step("test.read"), answer()])
    async with AgentRuntime(
        store, provider, CountingTool(Path(counter)), fault=runtime_fault
    ) as runtime:
        await runtime.run_turn(UUID(thread), "进程故障测试", request_id="crash")

    raise AssertionError("故障点未触发")


def crash(name: str) -> None:
    if name == sys.argv[3]:
        os._exit(77)


if __name__ == "__main__":
    asyncio.run(main())
