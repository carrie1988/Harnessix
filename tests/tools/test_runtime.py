from __future__ import annotations

import asyncio
import os
from threading import Event

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass
from harnessix.tools import files
from harnessix.tools.contracts import ListFilesOutput
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import call, execute


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool_version", "wrong"),
        ("tool_fingerprint", "0" * 64),
        ("effect_class", EffectClass.DESTRUCTIVE),
        ("requires_approval", True),
    ],
)
async def test_execution_rechecks_trusted_contract(tmp_path, field, value):
    async with CodingToolRuntime(tmp_path) as tools:
        forged = call(tools, path="x").model_copy(update={field: value})
        with pytest.raises(KernelError, match="已变化"):
            await tools.execute(forged, CancelToken())


async def test_definitions_are_isolated_and_unknown_tools_fail(tmp_path):
    async with CodingToolRuntime(tmp_path) as tools:
        before = tools.definitions()
        before[0].input_schema.clear()
        assert tools.definitions()[0].input_schema
        assert {d.name for d in tools.definitions()} == {"list_files", "read_file", "glob", "grep"}
        forged = call(tools, path="x").model_copy(update={"tool": "shell"})
        assert (await tools.execute(forged, CancelToken())).error.code == "unknown_tool"
    with pytest.raises(KernelError, match="已关闭"):
        await execute(tools, path="x")


async def test_output_contract_bug_is_not_a_normal_failure(tmp_path, monkeypatch):
    invalid = ListFilesOutput(
        path=".", entries=(), revision="0" * 64, truncated=False, next_offset=None
    )
    monkeypatch.setattr(files, "read_file", lambda *args: invalid)
    async with CodingToolRuntime(tmp_path) as tools:
        with pytest.raises(KernelError) as error:
            await execute(tools, path="x")
        assert error.value.code == "tool_output_invalid"


@pytest.mark.parametrize("kind", ["task", "token"])
async def test_cancel_waits_for_worker_and_closes_all_fds(tmp_path, monkeypatch, kind):
    (tmp_path / "x").write_text("内容\n")
    entered, stopped = asyncio.Event(), asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    opened = set()
    original_open = os.open
    original_read = files.read_file
    original_decode = files._decode

    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def block(data):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "测试未释放读取线程"
        return original_decode(data)

    def read(workspace, args, operation):
        original_set = operation.stopped.set

        def signal_stop():
            original_set()
            stopped.set()

        operation.stopped.set = signal_stop
        return original_read(workspace, args, operation)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(files, "read_file", read)
    monkeypatch.setattr(files, "_decode", block)
    token = CancelToken()
    async with CodingToolRuntime(tmp_path) as tools:
        task = asyncio.create_task(tools.execute(call(tools, path="x"), token))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if kind == "task":
                task.cancel()
            else:
                token.cancel()
            await asyncio.wait_for(stopped.wait(), 10)
            assert not task.done(), "线程尚未释放 FD，不得提前发布取消"
            if kind == "task":
                task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError if kind == "task" else TurnCancelled):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


async def test_cancel_queued_read_never_starts_second_worker(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    calls = []
    original = files.read_file
    (tmp_path / "x").write_text("测试")

    def block(*args):
        calls.append(1)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(files, "read_file", block)
    async with CodingToolRuntime(tmp_path) as tools:
        first = asyncio.create_task(execute(tools, path="x"))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            second = asyncio.create_task(execute(tools, path="x"))
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


async def test_precancelled_token_does_not_execute(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "read_file", lambda *args: pytest.fail("不应执行"))
    async with CodingToolRuntime(tmp_path) as tools:
        token = CancelToken()
        token.cancel()
        with pytest.raises(TurnCancelled):
            await tools.execute(call(tools, path="x"), token)


async def test_cancel_close_still_waits_for_active_scope_and_releases_root(tmp_path):
    tools = CodingToolRuntime(tmp_path)
    fd = tools._workspace._root_fd
    await tools._lock.acquire()
    closing = asyncio.create_task(tools.aclose())
    try:
        await asyncio.sleep(0)
        assert tools._closed and not closing.done()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done(), "关闭取消不得遗留根 FD 或跳过活跃执行的清理"
        closing.cancel()
        tools._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        if tools._lock.locked():
            tools._lock.release()
        await asyncio.gather(closing, return_exceptions=True)
        tools._workspace.close()
