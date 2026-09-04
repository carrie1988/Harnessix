import asyncio
import signal

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.processes.contracts import ProcessLimits
from tests.processes.helpers import cleanup, ready, request, running, runtime, stopped


@pytest.mark.parametrize("close_pipes", [False, True])
async def test_parent_exit_still_terminates_same_group_child(tmp_path, close_pipes):
    marker = tmp_path / "child"
    code = """
import os, signal, sys, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if sys.argv[2] == 'close':
        os.close(1); os.close(2)
    with open(sys.argv[1], 'w') as f: f.write(str(os.getpid()))
    time.sleep(30)
else:
    while not os.path.exists(sys.argv[1]): time.sleep(.001)
    os._exit(0)
"""
    pid = None
    try:
        async with runtime(tmp_path, limits=ProcessLimits(terminate_grace_seconds=0.05)) as host:
            result = await host.run(
                request(code, str(marker), "close" if close_pipes else "open"), CancelToken()
            )
        pid = await ready(marker)
        assert result.returncode == 0 and result.stop_reason == "exited"
        assert result.termination == "kill"
        assert result.stdout.eof and result.stderr.eof
        await stopped(pid)
    finally:
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        await cleanup(pid)


async def test_timeout_reaps_root_and_stops_same_group_grandchild(tmp_path):
    child, grandchild = tmp_path / "child", tmp_path / "grandchild"
    code = """
import os,signal,sys,time
if os.fork() == 0:
    with open(sys.argv[1],'w') as f: f.write(str(os.getpid()))
    if os.fork() == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(sys.argv[2],'w') as f: f.write(str(os.getpid()))
    time.sleep(30)
else: time.sleep(30)
"""
    pids = []
    task = None
    try:
        async with runtime(tmp_path, limits=ProcessLimits(terminate_grace_seconds=0.05)) as host:
            task = asyncio.create_task(
                host.run(request(code, str(child), str(grandchild), timeout=0.5), CancelToken())
            )
            pids = [await ready(child), await ready(grandchild)]
            result = await task
        assert result.stop_reason == "timeout" and result.termination == "kill"
        for pid in [result.pid, *pids]:
            await stopped(pid)
    finally:
        if task:
            await asyncio.gather(task, return_exceptions=True)
        for marker in (child, grandchild):
            if marker.exists():
                await cleanup(int(marker.read_text()))


async def test_detached_pipe_holder_does_not_hang_or_claim_containment(tmp_path):
    marker = tmp_path / "escaped"
    code = """
import os,sys,time
if os.fork() == 0:
    os.setsid()
    with open(sys.argv[1],'w') as f: f.write(str(os.getpid()))
    time.sleep(30)
else:
    while not os.path.exists(sys.argv[1]): time.sleep(.001)
    os._exit(0)
"""
    pid = None
    try:
        async with runtime(tmp_path, limits=ProcessLimits(pipe_drain_seconds=0.05)) as host:
            result = await host.run(request(code, str(marker)), CancelToken())
        pid = await ready(marker)
        assert result.returncode == 0 and result.stop_reason == "exited"
        assert result.termination == "none" and result.elapsed_seconds < 3
        assert not result.stdout.eof and not result.stderr.eof
        assert await asyncio.to_thread(running, pid)  # 显式验证不是OS Sandbox，测试自身负责清理。
    finally:
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        await cleanup(pid)


@pytest.mark.parametrize("how", ["task", "token", "close"])
async def test_cancel_during_spawn_keeps_handle_until_cleanup(tmp_path, monkeypatch, how):
    loop = asyncio.get_running_loop()
    original = loop.subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    pids = []

    async def delayed(*args, **kwargs):
        value = await original(*args, **kwargs)
        pids.append(value[0].get_pid())
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(loop, "subprocess_exec", delayed)
    host, token = runtime(tmp_path), CancelToken()
    task = asyncio.create_task(host.run(request("import time; time.sleep(30)"), token))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if how == "task":
            task.cancel()
        elif how == "token":
            token.cancel()
        else:
            closing = asyncio.create_task(host.aclose())
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        if how == "task":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).stop_reason == ("closed" if how == "close" else "cancelled")
        if closing:
            await closing
        await stopped(pids[0])
    finally:
        release.set()
        await host.aclose()
        await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)
        for pid in pids:
            await cleanup(pid)


async def test_cancelling_close_repeatedly_still_reaps(tmp_path):
    marker = tmp_path / "started"
    host = runtime(tmp_path)
    task = asyncio.create_task(
        host.run(
            request(
                "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)",
                str(marker),
            ),
            CancelToken(),
        )
    )
    closing = None
    try:
        pid = await ready(marker)
        closing = asyncio.create_task(host.aclose())
        await asyncio.sleep(0.01)
        closing.cancel()
        await asyncio.sleep(0.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert (await task).stop_reason == "closed"
        await stopped(pid)
    finally:
        await host.aclose()
        await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)
        if marker.exists():
            await cleanup(int(marker.read_text()))


async def test_group_signal_failure_is_not_success_and_closes_admission(tmp_path, monkeypatch):
    async with runtime(tmp_path) as host:
        monkeypatch.setattr(host, "_signal", lambda *_: False)
        result = await host.run(request("import time; time.sleep(30)", timeout=0.1), CancelToken())
        assert result.stop_reason == "cleanup_failed" and result.termination == "failed"
        assert result.returncode == -signal.SIGKILL
        with pytest.raises(KernelError) as error:
            await host.run(request("pass"), CancelToken())
        assert error.value.code == "process_closed"


async def test_signal_exit_retains_actual_negative_code(tmp_path):
    async with runtime(tmp_path) as host:
        result = await host.run(
            request("import os,signal; os.kill(os.getpid(),signal.SIGUSR1)"), CancelToken()
        )
    assert result.returncode == -signal.SIGUSR1 and result.stop_reason == "exited"


async def test_pipe_eof_is_not_process_exit(tmp_path):
    async with runtime(tmp_path) as host:
        result = await host.run(
            request("import os,time; os.close(1); os.close(2); time.sleep(30)", timeout=0.2),
            CancelToken(),
        )
    assert result.stop_reason == "timeout" and result.returncode < 0
    assert result.stdout.eof and result.stderr.eof
