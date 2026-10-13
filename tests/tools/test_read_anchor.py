"""唯一锚点读取的旧契约、稳定快照、分页与失败边界。"""

import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.tools import files
from harnessix.tools.contracts import (
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    ReadFileInputV2,
    ReadFileInputV3,
    ReadFileSnapshotOutput,
    ReadToolError,
)
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.windows_read import WindowsReadRuntime
from harnessix.tools.workspace import ReadOperation
from tests.tools.test_files import execute
from tests.tools.test_windows_read_adapter import _FakeWindowsRoot


def test_new_schema_keeps_both_frozen_input_versions():
    root = Path(__file__).parents[2] / "spec"
    assert json.loads((root / "read-file-input-v2.schema.json").read_text()) == (
        ReadFileInputV2.model_json_schema()
    )
    assert json.loads((root / "read-file-input-v3.schema.json").read_text()) == (
        ReadFileInputV3.model_json_schema()
    )
    assert "anchor" not in ReadFileInputV2.model_json_schema()["properties"]


@pytest.mark.parametrize("before", [0, 2, 40])
async def test_exact_anchor_page_has_full_raw_digest_and_continuation(tmp_path, before):
    body = ("# 前文\r\n" * 45 + "def target():\r\n" + "    return 'é中文'\n" * 10).encode()
    path = tmp_path / "source.py"
    path.write_bytes(body)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(
            tools,
            path="source.py",
            anchor="def target():",
            lines_before=before,
            max_lines=before + 2,
            max_utf8_bytes=4096,
        )
        assert result.outcome == "succeeded"
        page = ReadFileSnapshotOutput.model_validate_json(json.dumps(result.output))
        assert page.start_line == 46 - before and page.end_line == 47
        assert "def target():\r\n" in page.text and page.next_line == 48
        assert page.digest_status == "complete"
        assert page.content_sha256 == hashlib.sha256(body).hexdigest()
        continued = await execute(
            tools,
            path="source.py",
            start_line=page.next_line,
            expected_revision=page.revision,
            max_utf8_bytes=4096,
        )
        assert continued.outcome == "succeeded" and continued.output["start_line"] == 48
    assert path.read_bytes() == body


@pytest.mark.parametrize(
    "arguments",
    [
        {"anchor": ""},
        {"anchor": True},
        {"anchor": "line\nline"},
        {"anchor": "tab\there"},
        {"anchor": "中" * 86},
        {"anchor": "x", "start_line": 2},
        {"lines_before": 1},
        {"anchor": "x", "lines_before": True},
        {"anchor": "x", "lines_before": 1.0},
        {"anchor": "x", "lines_before": 41},
        {"anchor": "x", "lines_before": -1},
    ],
)
async def test_invalid_anchor_contract_fails_before_io(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(files, "read_file_snapshot", lambda *a: pytest.fail("不得读取文件"))
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, path="source.py", **arguments)
    assert result.error.code == "tool_invalid_arguments" and result.output is None


@pytest.mark.parametrize(
    "body,anchor,error",
    [
        (b"", "target", "anchor_not_found"),
        (b"Target\n", "target", "anchor_not_found"),
        (b"def other():\n", "def target", "anchor_not_found"),
        (b"target\ntarget\n", "target", "anchor_ambiguous"),
        (b"target target\n", "target", "anchor_ambiguous"),
        (b"aaa\n", "aa", "anchor_ambiguous"),
        (b"target\n\xff", "target", "invalid_utf8"),
        (b"target\n\x00", "target", "binary_file"),
        (b"x" * (MAX_LINE_BYTES + 1), "x", "limit_exceeded"),
        (b"target\n" + b"x\n" * (MAX_SCAN_BYTES // 2), "target", "limit_exceeded"),
    ],
)
@pytest.mark.parametrize("native", [False, True])
async def test_complete_scan_failures_never_guess_or_return_partial_digest(
    tmp_path,
    body,
    anchor,
    error,
    native,
):
    path = tmp_path / "source.py"
    path.write_bytes(body)
    if native:
        tools = WindowsReadRuntime(tmp_path, root_factory=cast(Any, _FakeWindowsRoot))
        try:
            with pytest.raises(ReadToolError) as failure:
                tools.read_file_snapshot(
                    ReadFileInputV3(path="source.py", anchor=anchor), ReadOperation()
                )
            assert failure.value.code == error
        finally:
            tools.close()
    else:
        async with CodingToolRuntime(tmp_path) as tools:
            result = await execute(tools, path="source.py", anchor=anchor)
        assert result.error.code == f"tool_{error}" and result.output is None
    assert path.read_bytes() == body


async def test_anchor_must_be_visible_in_requested_page(tmp_path):
    (tmp_path / "source.py").write_text("prefix\n" * 20 + "target\n")
    async with CodingToolRuntime(tmp_path) as tools:
        hidden = await execute(
            tools, path="source.py", anchor="target", lines_before=20, max_lines=1
        )
        assert hidden.error.code == "tool_anchor_outside_page" and hidden.output is None
        visible = await execute(tools, path="source.py", anchor="target", max_lines=1)
        assert visible.outcome == "succeeded" and visible.output["start_line"] == 21


async def test_byte_limit_cannot_hide_anchor_behind_context(tmp_path):
    (tmp_path / "source.py").write_text("x" * 3999 + "\n" + "y" * 3999 + "\ntarget\n")
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(
            tools,
            path="source.py",
            anchor="target",
            lines_before=2,
            max_utf8_bytes=4096,
        )
    assert result.error.code == "tool_anchor_outside_page" and result.output is None


async def test_mutation_between_anchor_scan_and_page_discards_result(tmp_path, monkeypatch):
    path = tmp_path / "source.py"
    path.write_text("target\n")
    resolve = files._resolve_fd_anchor

    def change(fd, args, operation):
        result = resolve(fd, args, operation)
        path.write_text("changed target\n")
        return result

    monkeypatch.setattr(files, "_resolve_fd_anchor", change)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, path="source.py", anchor="target")
    assert result.error.code == "tool_workspace_changed" and result.output is None


async def test_stale_revision_cannot_resolve_anchor(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("target\n")
    async with CodingToolRuntime(tmp_path) as tools:
        first = await execute(tools, path="source.py", anchor="target")
        path.write_text("prefix\ntarget\n")
        changed = await execute(
            tools, path="source.py", anchor="target", expected_revision=first.output["revision"]
        )
    assert changed.error.code == "tool_page_changed" and changed.output is None


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_anchor_full_scan_honors_control_after_first_hit(stop):
    operation = ReadOperation()

    class InterruptedBytes(io.BytesIO):
        def readline(self, size=-1):
            raw = super().readline(size)
            if stop == "cancel":
                operation.stopped.set()
            else:
                operation.deadline = 0
            return raw

    with InterruptedBytes(b"target\nmore\n") as stream:
        with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as stopped:
            files._resolve_read_anchor(
                stream, ReadFileInputV3(path="source.py", anchor="target"), operation
            )
        if stop == "timeout":
            assert stopped.value.code == "timeout"


async def test_omitted_anchor_preserves_v2_behavior_and_native_snapshot(tmp_path):
    body = ("# 行\r\n" * 60 + "target\n").encode()
    (tmp_path / "source.py").write_bytes(body)
    async with CodingToolRuntime(tmp_path) as tools:
        old = await execute(
            tools, path="source.py", start_line=20, max_lines=5, max_utf8_bytes=4096
        )
        explicit = await execute(
            tools, path="source.py", start_line=20, max_lines=5, max_utf8_bytes=4096, anchor=None
        )
        found = await execute(tools, path="source.py", anchor="target", lines_before=2)
        assert old.output == explicit.output
    native = WindowsReadRuntime(tmp_path, root_factory=cast(Any, _FakeWindowsRoot))
    try:
        page = native.read_file_snapshot(
            ReadFileInputV3(path="source.py", anchor="target", lines_before=2), ReadOperation()
        )
        for field in ("text", "start_line", "end_line", "content_sha256", "digest_status"):
            assert getattr(page, field) == found.output[field]
    finally:
        native.close()
