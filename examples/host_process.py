"""受信宿主进程验收：不启用模型Shell，不执行任意仓库代码。"""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.cancellation import CancelToken
from harnessix.processes.contracts import ProcessLimits, ProcessRequest
from harnessix.processes.runtime import HostProcessRuntime


async def exercise(root: Path) -> None:
    async with HostProcessRuntime(
        root,
        {"python": sys.executable},
        limits=ProcessLimits(stdout_bytes=32, stderr_bytes=32, terminate_grace_seconds=0.05),
    ) as host:
        result = await host.run(
            ProcessRequest(
                program="python",
                arguments=(
                    "-I",
                    "-c",
                    "import os; os.write(1,b'a'*100000); os.write(2,b'checked')",
                ),
            ),
            CancelToken(),
        )
        assert result.returncode == 0 and result.stop_reason == "exited"
        assert result.stdout.captured_bytes == 32 and result.stdout.observed_bytes == 100000
        assert result.stdout.truncated and result.stdout.eof
        assert result.stderr.text() == "checked" and result.stderr.eof
        timeout = await host.run(
            ProcessRequest(
                program="python",
                arguments=("-I", "-c", "import time; time.sleep(10)"),
                timeout_seconds=0.2,
            ),
            CancelToken(),
        )
        assert timeout.stop_reason == "timeout" and timeout.returncode < 0
    print("宿主进程：双流有界捕获/完整排水、超时终止与直接子进程回收通过。")
    print("没有启用模型Shell、持久命令审批或OS Sandbox；没有模型请求或中间件。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-host-process-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
