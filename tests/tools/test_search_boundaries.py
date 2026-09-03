from __future__ import annotations

import asyncio
import errno
import os
from threading import Event

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.tools import search
from harnessix.tools.runtime import CodingToolRuntime
from tests.tools.test_files import call, execute


@pytest.mark.parametrize(
    "tool,args,field", [("glob", {}, "paths"), ("grep", {"query": "needle"}, "matches")]
)
async def test_ignore_is_not_permission_and_links_never_escape(tmp_path, tool, args, field):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside").write_text("needle PRIVATE")
    for path in ("a.py", ".env", ".hidden.py", "private", "node_modules/a.py", ".git/a.py"):
        file = root / path
        file.parent.mkdir(exist_ok=True)
        file.write_text("needle")
    (root / "link").symlink_to(tmp_path / "outside")
    (root / "dirlink").symlink_to(tmp_path)
    os.link(tmp_path / "outside", root / "hardlink")
    os.mkfifo(root / "fifo")
    async with CodingToolRuntime(root, denied_paths=("private",)) as tools:

        def paths(result):
            return (
                result.output[field]
                if tool == "glob"
                else [m["path"] for m in result.output[field]]
            )

        normal = await execute(tools, tool, **args)
        assert paths(normal) == [".hidden.py", "a.py"]
        assert normal.output["scan_complete"]
        ignored = await execute(tools, tool, include_ignored=True, **args)
        assert paths(ignored) == [".hidden.py", "a.py", "node_modules/a.py"]
        explicit = await execute(tools, tool, path="node_modules", **args)
        assert paths(explicit) == ["node_modules/a.py"]
        for denied in (".git", "../", "dirlink", "private"):
            result = await execute(tools, tool, path=denied, include_ignored=True, **args)
            assert result.error.code == "tool_path_denied"
            assert "PRIVATE" not in result.model_dump_json()


async def test_subdirectory_patterns_and_literal_not_regex(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/x.py").write_text("xABC\n^x.*$\r\n")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", path="src", include="*.py", query="^x.*$")
        assert [(m["path"], m["line"], m["text"]) for m in result.output["matches"]] == [
            ("src/x.py", 2, "^x.*$")
        ]
        assert result.output["scan_complete"]


async def test_unterminated_line_retains_its_literal_carriage_return(tmp_path):
    (tmp_path / "x").write_bytes(b"needle\r")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle")
        hit = result.output["matches"][0]
        assert hit["text"] == "needle\r" and not hit["text_truncated"]


async def test_binary_invalid_utf8_large_file_and_long_lines_are_visible_gaps(
    tmp_path, monkeypatch
):
    (tmp_path / "a").write_bytes(b"needle\n\xff")
    (tmp_path / "b").write_bytes(b"needle\n\x00")
    (tmp_path / "c").write_bytes(b"x" * 9000)
    (tmp_path / "d").write_bytes(b"needle" + b"x" * 4100 + b"\nneedle\n")
    monkeypatch.setattr(search, "MAX_SEARCH_FILE_BYTES", 8000)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle")
        assert not result.output["scan_complete"] and not result.output["truncated"]
        assert [(m["path"], m["line"]) for m in result.output["matches"]] == [("d", 2)]
        stats = result.output["stats"]
        assert stats["invalid_utf8_files"] == stats["binary_files"] == 1
        assert stats["oversized_files"] == stats["long_lines"] == 1


async def test_unicode_preview_contains_entire_query_on_character_boundaries(tmp_path):
    query = "q" * 256
    source = "前" * 800 + query + "后" * 300
    (tmp_path / "x").write_text(source + "\n", encoding="utf-8")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query=query)
        hit = result.output["matches"][0]
        assert query in hit["text"] and hit["text"] in source
        assert len(hit["text"].encode()) <= search.MAX_MATCH_BYTES
        assert hit["text_truncated"] and result.output["scan_complete"]


@pytest.mark.parametrize(
    "tool,args,field", [("glob", {}, "paths"), ("grep", {"query": "q"}, "matches")]
)
async def test_empty_and_exact_result_boundaries(tmp_path, tool, args, field):
    async with CodingToolRuntime(tmp_path) as tools:
        empty = await execute(tools, tool, **args)
        assert empty.output[field] == [] and empty.output["scan_complete"]
        for name in ("b", "a"):
            (tmp_path / name).write_text("q\n")
        exact = await execute(tools, tool, max_results=2, **args)
        assert len(exact.output[field]) == 2 and exact.output["scan_complete"]


@pytest.mark.parametrize(
    "budget",
    ["MAX_SEARCH_ENTRIES", "MAX_SEARCH_NAMES_BYTES", "MAX_SEARCH_DEPTH", "MAX_SEARCH_TOTAL_BYTES"],
)
async def test_hard_scan_budget_never_claims_partial_success(tmp_path, monkeypatch, budget):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/q").write_text("q" * 50)
    (tmp_path / "a").write_text("q" * 50)
    monkeypatch.setattr(search, budget, 0 if budget == "MAX_SEARCH_DEPTH" else 1)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="q")
        assert result.error.code == "tool_limit_exceeded" and result.output is None


@pytest.mark.parametrize(
    "tool,args,field,limit", [("glob", {}, "paths", 50), ("grep", {"query": "q"}, "matches", 200)]
)
async def test_output_byte_limit_is_explicit(tmp_path, monkeypatch, tool, args, field, limit):
    for name in ("a" * 25, "b" * 25, "c" * 25):
        (tmp_path / name).write_text("q")
    monkeypatch.setattr(search, "MAX_SEARCH_RECORD_BYTES", limit)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, tool, **args)
        assert len(result.output[field]) == 1
        assert result.output["truncation_reason"] == "output_limit"
        assert not result.output["scan_complete"]


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
async def test_discovered_object_replacement_fails_before_read(tmp_path, monkeypatch, kind):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/a").write_text("original")
    real_collect = search._collect

    def replace(*args):
        nodes = real_collect(*args)
        if kind == "directory":
            (tmp_path / "sub").rename(tmp_path / "old")
            (tmp_path / "sub").mkdir()
        else:
            (tmp_path / "sub/a").rename(tmp_path / "sub/old")
        if kind == "symlink":
            (tmp_path / "sub/a").symlink_to(tmp_path / "sub/old")
        else:
            (tmp_path / "sub/a").write_text("PRIVATE replacement")
        return nodes

    monkeypatch.setattr(search, "_collect", replace)
    monkeypatch.setattr(search, "_decode", lambda _: pytest.fail("不得读取替换对象"))
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="original")
        assert result.error.code == "tool_workspace_changed"
        assert "PRIVATE" not in result.model_dump_json()


async def test_changed_file_during_search_discards_earlier_matches(tmp_path, monkeypatch):
    for name in ("a", "b"):
        (tmp_path / name).write_text("needle\n")
    original = search._read
    count = 0

    def change(*args):
        nonlocal count
        text = original(*args)
        count += 1
        if count == 2:
            (tmp_path / "b").write_text("changed")
        return text

    monkeypatch.setattr(search, "_read", change)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle")
        assert count == 2 and result.error.code == "tool_workspace_changed"
        assert result.output is None


async def test_permission_gap_and_io_failure_are_not_hidden(tmp_path, monkeypatch):
    for name in ("a", "b"):
        (tmp_path / name).write_text("needle")
    real_open = os.open

    def denied(path, *args, **kwargs):
        if path == "b":
            raise PermissionError(errno.EACCES, "PRIVATE /secret")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, "grep", query="needle")
        assert [m["path"] for m in result.output["matches"]] == ["a"]
        assert result.output["stats"]["unreadable_entries"] == 1
        assert not result.output["scan_complete"]
        assert (await execute(tools, path="b")).error.code == "tool_path_denied"
        assert "PRIVATE" not in result.model_dump_json()

        def io_error(*args):
            raise OSError(errno.EIO, "PRIVATE")

        monkeypatch.setattr(search, "_read", io_error)
        failed = await execute(tools, "grep", query="needle")
        assert failed.error.code == "tool_io_failed" and "PRIVATE" not in failed.model_dump_json()


@pytest.mark.parametrize("kind", ["task", "token"])
async def test_search_cancel_waits_for_file_descriptor_cleanup(tmp_path, monkeypatch, kind):
    (tmp_path / "x").write_text("needle")
    entered, stopped = asyncio.Event(), asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    observed = []
    real_read = search._read

    def block(fd, operation, scan):
        observed.append(fd)
        stop = operation.stopped.set

        def notify():
            stop()
            stopped.set()

        operation.stopped.set = notify
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        operation.checkpoint()
        return real_read(fd, operation, scan)

    monkeypatch.setattr(search, "_read", block)
    async with CodingToolRuntime(tmp_path) as tools:
        token = CancelToken()
        task = asyncio.create_task(tools.execute(call(tools, "grep", query="needle"), token))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if kind == "task":
                task.cancel()
            else:
                token.cancel()
            await asyncio.wait_for(stopped.wait(), 10)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError if kind == "task" else TurnCancelled):
                await task
            with pytest.raises(OSError):
                os.fstat(observed[0])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_search_cooperative_deadline(tmp_path, monkeypatch):
    collect = search._collect

    def expired(workspace, args, operation, scan):
        operation.deadline = 0
        return collect(workspace, args, operation, scan)

    monkeypatch.setattr(search, "_collect", expired)
    async with CodingToolRuntime(tmp_path) as tools:
        assert (await execute(tools, "glob")).error.code == "tool_timeout"
