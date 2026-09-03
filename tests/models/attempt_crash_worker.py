from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.session.sqlite import SQLiteSessionStore
from tests.models.attempt_helpers import CANARY, KEY_ENV, Adapter


async def main() -> None:
    database, thread_id, kind, point = sys.argv[1:]
    os.environ[KEY_ENV] = CANARY
    for name in ("OPENAI_CUSTOM_HEADERS", "ANTHROPIC_CUSTOM_HEADERS"):
        os.environ.pop(name, None)
    adapter = Adapter(kind)
    wire = adapter.wire.WireStream(adapter.wire.text_frames())
    observations = 0

    def crash(name):
        nonlocal observations
        if name == "runtime.after_model_usage_observed":
            observations += 1
        target = {
            "started": name == "runtime.after_model_attempt_started",
            "initial_usage": name == "runtime.after_model_usage_observed" and observations == 1,
            "complete_usage": name == "runtime.after_model_usage_observed"
            and observations == (2 if kind == "openai" else 3),
            "finished": name == "runtime.after_model_attempt_finished",
        }[point]
        if target:
            os._exit(77)

    async def handle(request):
        await asyncio.to_thread(Path(database + ".requests").write_text, "1")
        return adapter.wire.response(wire)

    async with adapter.provider(wire, handler=handle) as provider:
        async with AgentRuntime(SQLiteSessionStore(database), provider, fault=crash) as runtime:
            await runtime.run_turn(UUID(thread_id), "SDK 崩溃切点", request_id="crash")
    raise AssertionError("未触发 SDK 崩溃切点")


if __name__ == "__main__":
    asyncio.run(main())
