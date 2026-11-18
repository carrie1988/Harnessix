import asyncio
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import harnessix
from harnessix.processes import owner_standby
from harnessix.processes.contracts import ProcessRequest
from harnessix.processes.runtime import HostProcessRuntime


def request(code, *arguments, timeout=5.0):
    return ProcessRequest(
        program="python", arguments=("-I", "-c", code, *arguments), timeout_seconds=timeout
    )


def runtime(root, **kwargs):
    return HostProcessRuntime(root, {"python": sys.executable}, **kwargs)


def _ready(path: Path):
    return (
        int(path.read_text(encoding="utf-8"))
        if path.exists() and path.read_text(encoding="utf-8").strip()
        else None
    )


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


def zombie_leader() -> subprocess.Popen[bytes]:
    """独立进程组里已退出、尚未被wait回收的组长。"""

    read_end, write_end = os.pipe()
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        start_new_session=True,
        pass_fds=(write_end,),
    )
    os.close(write_end)
    os.killpg(process.pid, signal.SIGTERM)
    # 子进程退出时关闭继承的写端，读端随即EOF；不wait，组长保持僵尸。
    ready_ends, _, _ = select.select([read_end], [], [], 10)
    assert ready_ends and os.read(read_end, 1) == b"", "子进程未按时退出"
    os.close(read_end)
    time.sleep(0.2)
    return process


def owner_imports_this_checkout(monkeypatch) -> None:
    """让真实POSIX Owner进程导入与测试进程同一份harnessix。

    Owner以固定环境启动，经解释器的site-packages找到harnessix；几个工作树共用一个可编辑安装时，
    那里指向的可能是另一棵树，Owner一侧的改动就没有被测到。这里只为测试补上PYTHONPATH，
    产品启动Owner的方式不变；Owner交给目标进程的仍是Start帧里的环境。
    """

    source = str(Path(harnessix.__file__).resolve().parents[1])
    environment = {**owner_standby._OWNER_ENVIRONMENT, "PYTHONPATH": source}
    monkeypatch.setattr(owner_standby, "_OWNER_ENVIRONMENT", environment)
