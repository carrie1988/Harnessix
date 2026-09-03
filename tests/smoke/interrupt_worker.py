"""真实 SIGINT 验收子进程；两个 SDK 都只使用离线传输。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from harnessix.cli import main
from harnessix.smoke import runner
from tests.smoke.helpers import CANARY, WireFactory, config


def worker() -> None:
    provider, result_path, config_path = sys.argv[1:]
    os.environ["HARNESSIX_SMOKE_TEST_KEY"] = CANARY
    os.environ.pop("OPENAI_CUSTOM_HEADERS", None)
    os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    cfg = config(provider)
    Path(config_path).write_text(cfg.model_dump_json())
    factory = WireFactory(fault="cancel")

    class InspectRuntime(runner.AgentRuntime):
        async def __aexit__(self, *args):
            await super().__aexit__(*args)
            ids = await self.store.thread_ids()
            snapshot = await self.store.get_thread(ids[0])
            await asyncio.to_thread(
                Path(result_path).write_text,
                json.dumps({"workspace": snapshot.workspace, "status": snapshot.turns[0].status}),
            )

    async def interrupt():
        await factory.request_entered.wait()
        await factory.wires[0].entered.wait()
        os.kill(os.getpid(), signal.SIGINT)

    @asynccontextmanager
    async def substitute(checked):
        async with factory(checked) as sdk:
            task = asyncio.create_task(interrupt())
            try:
                yield sdk
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    runner.AgentRuntime = InspectRuntime
    runner._sdk_provider = substitute
    main(["model-smoke", "--config", config_path, "--allow-network"])


if __name__ == "__main__":
    worker()
