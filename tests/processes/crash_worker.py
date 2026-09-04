"""硬退出的能力边界夹具；父测试负责回收仍存活的测试进程组。"""

import asyncio
import os
import sys
from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.processes.contracts import ProcessRequest
from harnessix.processes.runtime import HostProcessRuntime
from tests.processes.helpers import _ready


async def main():
    marker = Path(sys.argv[1])
    async with HostProcessRuntime(marker.parent, {"python": sys.executable}) as host:
        task = asyncio.create_task(
            host.run(
                ProcessRequest(
                    program="python",
                    arguments=(
                        "-I",
                        "-c",
                        "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); "
                        "time.sleep(30)",
                        str(marker),
                    ),
                ),
                CancelToken(),
            )
        )
        for _ in range(500):
            if await asyncio.to_thread(_ready, marker) is not None:
                os._exit(84)
            if task.done():
                await task
                raise AssertionError("进程过早退出")
            await asyncio.sleep(0.01)
        raise AssertionError("未到达硬退出窗口")


if __name__ == "__main__":
    asyncio.run(main())
