from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from threading import Event

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.tools import files
from harnessix.tools.contracts import (
    MAX_SCAN_BYTES,
    ReadFileInput,
    ReadFileSnapshotOutput,
    ReadToolError,
)
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.tools.test_files import call, execute

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX文件快照端口")


@pytest.mark.parametrize(
    "content",
    [b"", b"tail", b"first\r\nsecond\n", "中文\n尾行".encode(), b"\xef\xbb\xbfprint(1)\n"],
)
async def test_read_tool_exposes_complete_raw_content_digest(
    tmp_path: Path, content: bytes
) -> None:
    (tmp_path / "app.py").write_bytes(content)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, path="app.py", max_lines=1)
        assert result.outcome == "succeeded"
        assert result.output["spec_version"] == "harnessix.read-file-snapshot/v1"
        assert result.output["content_sha256"] == hashlib.sha256(content).hexdigest()
        assert result.output["file_bytes"] == len(content)
        assert result.output["digest_status"] == "complete"
        assert result.output["revision"] != result.output["content_sha256"]
        definition = next(tool for tool in tools.definitions() if tool.name == "read_file")
        assert definition.version.startswith("2.")


async def test_digest_covers_full_file_even_when_text_is_paginated(tmp_path: Path) -> None:
    body = b"first\nsecond\nthird\n"
    (tmp_path / "app.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path) as tools:
        first = await execute(tools, path="app.py", max_lines=1)
        assert first.output["text"] == "first\n" and first.output["truncated"]
        assert first.output["content_sha256"] != hashlib.sha256(b"first\n").hexdigest()
        second = await execute(
            tools, path="app.py", start_line=2, expected_revision=first.output["revision"]
        )
        assert second.output["text"] == "second\nthird\n"
        assert second.output["content_sha256"] == first.output["content_sha256"]


async def test_large_file_remains_readable_without_fabricated_digest(tmp_path: Path) -> None:
    body = b"x\n" * (MAX_SCAN_BYTES // 2 + 1)
    (tmp_path / "large.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, path="large.py", max_lines=1)
        assert result.outcome == "succeeded" and result.output["text"] == "x\n"
        assert result.output["file_bytes"] == len(body)
        assert result.output["digest_status"] == "omitted_limit"
        assert result.output["content_sha256"] is None


def test_legacy_read_library_contract_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_bytes(b"unchanged\n")
    with Workspace(tmp_path) as workspace:
        page = files.read_file(workspace, ReadFileInput(path="app.py"), ReadOperation())
        assert set(page.model_dump()) == {
            "path",
            "text",
            "start_line",
            "end_line",
            "utf8_bytes",
            "revision",
            "truncated",
            "truncation_reason",
            "next_line",
        }


def test_exact_digest_limit_is_complete_and_larger_file_does_not_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"x\n" * (MAX_SCAN_BYTES // 2)
    target = tmp_path / "app.py"
    target.write_bytes(body)
    with Workspace(tmp_path) as workspace:
        result = files.read_file_snapshot(
            workspace, ReadFileInput(path="app.py", max_lines=1), ReadOperation()
        )
        assert result.digest_status == "complete" and result.file_bytes == MAX_SCAN_BYTES
        assert result.content_sha256 == hashlib.sha256(body).hexdigest()
        target.write_bytes(body + b"x\n")
        monkeypatch.setattr(
            files, "_complete_content_digest", lambda *_args: pytest.fail("超限文件不得扫描摘要")
        )
        result = files.read_file_snapshot(
            workspace, ReadFileInput(path="app.py", max_lines=1), ReadOperation()
        )
        assert result.digest_status == "omitted_limit" and result.content_sha256 is None


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_size_change_during_digest_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    target = tmp_path / "app.py"
    target.write_bytes(b"x\n" * 65536)
    original_read = os.read
    calls = 0

    def changing_read(fd: int, length: int) -> bytes:
        nonlocal calls
        data = original_read(fd, length)
        calls += 1
        if calls == 1:
            if mutation == "grow":
                with target.open("ab") as stream:
                    stream.write(b"new\n")
            else:
                target.write_bytes(b"short\n")
        return data

    monkeypatch.setattr(os, "read", changing_read)
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        files.read_file_snapshot(workspace, ReadFileInput(path="app.py"), ReadOperation())
    assert failed.value.code == "workspace_changed"


@pytest.mark.parametrize("mutation", ["rewrite", "replace"])
def test_change_between_digest_and_text_page_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    target = tmp_path / "app.py"
    target.write_bytes(b"old\n")
    before = target.stat()
    original = files._complete_content_digest

    def changing_digest(fd: int, file_bytes: int, operation: ReadOperation) -> str:
        result = original(fd, file_bytes, operation)
        if mutation == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"new\n")
            os.replace(replacement, target)
        else:
            target.write_bytes(b"new\n")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return result

    monkeypatch.setattr(files, "_complete_content_digest", changing_digest)
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        files.read_file_snapshot(workspace, ReadFileInput(path="app.py"), ReadOperation())
    assert failed.value.code == "workspace_changed"
    assert target.read_bytes() == b"new\n"


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_digest_checkpoints_stop_and_release_file_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    (tmp_path / "app.py").write_bytes(b"x\n" * 65536)
    original_read = os.read
    operation = ReadOperation()
    observed_fd: list[int] = []

    def stopping_read(fd: int, length: int) -> bytes:
        observed_fd.append(fd)
        data = original_read(fd, length)
        if stop == "cancel":
            operation.stopped.set()
        else:
            operation.deadline = time.monotonic() - 1
        return data

    monkeypatch.setattr(os, "read", stopping_read)
    with Workspace(tmp_path) as workspace:
        with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as failed:
            files.read_file_snapshot(workspace, ReadFileInput(path="app.py"), operation)
        if stop == "timeout":
            assert failed.value.code == "timeout"
        assert observed_fd
        with pytest.raises(OSError):
            os.fstat(observed_fd[0])


@pytest.mark.parametrize(
    "invalid",
    [
        {"digest_status": "omitted_limit"},
        {"content_sha256": None},
        {"content_sha256": "not-a-digest"},
        {"file_bytes": MAX_SCAN_BYTES + 1},
        {"file_bytes": 0},
        {"file_bytes": True},
        {"file_bytes": -1},
        {"digest_status": "partial"},
        {"spec_version": "harnessix.read-file-snapshot/v2"},
        {"unexpected": 1},
    ],
)
def test_snapshot_contract_rejects_inconsistent_or_forged_fields(
    tmp_path: Path, invalid: dict[str, object]
) -> None:
    (tmp_path / "app.py").write_bytes(b"x\n")
    with Workspace(tmp_path) as workspace:
        valid = files.read_file_snapshot(workspace, ReadFileInput(path="app.py"), ReadOperation())
    with pytest.raises(ValidationError):
        ReadFileSnapshotOutput.model_validate({**valid.model_dump(), **invalid})


async def test_previous_major_tool_contract_cannot_execute_new_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_bytes(b"x\n")
    monkeypatch.setattr(files, "read_file_snapshot", lambda *_args: pytest.fail("不得执行旧合同"))
    async with CodingToolRuntime(tmp_path) as tools:
        trusted = call(tools, path="app.py")
        assert trusted.tool_version.startswith("2.")
        assert all(d.version.startswith("1.") for d in tools.definitions() if d.name != "read_file")
        stale = trusted.model_copy(update={"tool_version": "1." + trusted.tool_version[2:]})
        with pytest.raises(KernelError) as failed:
            await tools.execute(stale, CancelToken())
    assert failed.value.code == "tool_contract_changed"


@pytest.mark.parametrize("kind", ["task", "token"])
async def test_cancellation_during_digest_waits_for_worker_and_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    (tmp_path / "app.py").write_bytes(b"x\n" * 65536)
    entered, stopped = asyncio.Event(), asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    original = files._complete_content_digest
    observed_fd: list[int] = []

    def blocked_digest(fd: int, size: int, operation: ReadOperation) -> str:
        observed_fd.append(fd)
        original_set = operation.stopped.set

        def signal_stop() -> None:
            original_set()
            loop.call_soon_threadsafe(stopped.set)

        operation.stopped.set = signal_stop
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "测试必须释放摘要工作线程"
        return original(fd, size, operation)

    monkeypatch.setattr(files, "_complete_content_digest", blocked_digest)
    token = CancelToken()
    async with CodingToolRuntime(tmp_path) as tools:
        task = asyncio.create_task(tools.execute(call(tools, path="app.py"), token))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if kind == "task":
                task.cancel()
            else:
                token.cancel()
            await asyncio.wait_for(stopped.wait(), 10)
            assert not task.done(), "FD仍由摘要线程拥有，不得提前发布取消"
            if kind == "task":
                task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError if kind == "task" else TurnCancelled):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(OSError):
            os.fstat(observed_fd[0])
