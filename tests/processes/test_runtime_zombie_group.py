"""结束命令时，已退出但未回收的组长不能被当成清理失败（HostProcessRuntime一侧）。

2026-11-11 CI macOS：test_output_stop_threshold_closes_pipes_without_claiming_eof仍得到
cleanup_failed。输出超限后管道被关闭，子进程因写入失败自行退出；asyncio在自己的线程里回收它，_settle先于回收
运行时，macOS对只剩僵尸的进程组killpg返回EPERM，被判为清理失败并关闭整个运行时。
b11只修了posix_owner的同类判断，这里是另一处。Linux此时killpg成功，测试照常通过。
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from harnessix.processes.capture import CaptureProtocol
from harnessix.processes.contracts import ProcessLimits
from tests.processes.helpers import runtime, zombie_leader

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX进程组语义")


async def test_settling_a_command_that_already_exited_is_not_a_cleanup_failure(tmp_path) -> None:
    limits = ProcessLimits(terminate_grace_seconds=0)
    process = zombie_leader()
    capture = CaptureProtocol(limits, lambda reason: None)
    transport = SimpleNamespace(get_pid=lambda: process.pid, close=lambda: None)

    async def reap_later() -> None:
        await asyncio.sleep(0.2)
        await asyncio.to_thread(process.wait)
        capture.process_exited()
        capture.connection_lost(None)

    reaper = asyncio.create_task(reap_later())
    async with runtime(tmp_path, limits=limits) as host:
        termination = await host._settle(transport, capture)
        await reaper
        assert termination != "failed"
        assert not host._closed
