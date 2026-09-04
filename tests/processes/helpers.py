import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from harnessix.processes.contracts import ProcessRequest
from harnessix.processes.runtime import HostProcessRuntime


def request(code, *arguments, timeout=5.0):
    return ProcessRequest(
        program="python", arguments=("-I", "-c", code, *arguments), timeout_seconds=timeout
    )


def runtime(root, **kwargs):
    return HostProcessRuntime(root, {"python": sys.executable}, **kwargs)


def _ready(path: Path):
    return int(path.read_text()) if path.exists() and path.read_text().strip() else None


async def ready(path: Path):
    # 等待独立OS进程，不能以本事件循环的Event替代跨进程观察。
    for _ in range(500):
        pid = await asyncio.to_thread(_ready, path)
        if pid is not None:
            return pid
        await asyncio.sleep(0.01)
    raise TimeoutError("子进程未报告就绪")


def running(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3
    )
    return (
        result.returncode == 0
        and bool(result.stdout.strip())
        and not result.stdout.strip().startswith("Z")
    )


async def stopped(pid):
    for _ in range(250):
        if not await asyncio.to_thread(running, pid):
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("测试进程仍在运行")


async def cleanup(pid, *, group=False):
    if pid is None:
        return
    try:
        (os.killpg if group else os.kill)(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await stopped(pid)
