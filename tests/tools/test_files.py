from __future__ import annotations

import errno
import os

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.ids import new_id
from harnessix.agent.models import ToolCallContent
from harnessix.tools import files
from harnessix.tools.contracts import MAX_LINE_BYTES, MAX_SCAN_BYTES, MAX_TEXT_BYTES
from harnessix.tools.runtime import CodingToolRuntime


def call(tools, name="read_file", **arguments):
    definition = next(d for d in tools.definitions() if d.name == name)
    return ToolCallContent(
        call_id=new_id(),
        provider_call_id="offline-call",
        tool=name,
        tool_version=definition.version,
        effect_class=definition.effect_class,
        arguments=arguments,
        requires_approval=definition.requires_approval,
        tool_fingerprint=tool_fingerprint(definition),
    )


async def execute(tools, name="read_file", **arguments):
    return await tools.execute(call(tools, name, **arguments), CancelToken())


async def test_read_utf8_pages_and_detect_change(tmp_path):
    file = tmp_path / "中文.py"
    file.write_bytes("第一行\r\n第二行\n尾行".encode())
    async with CodingToolRuntime(tmp_path) as tools:
        first = await execute(tools, path="中文.py", max_lines=1)
        assert first.outcome == "succeeded"
        page = first.output
        assert page["text"] == "第一行\r\n"
        assert page["utf8_bytes"] == 11
        assert page["end_line"] == 1 and page["next_line"] == 2
        assert page["truncation_reason"] == "line_limit"
        second = await execute(
            tools, path="中文.py", start_line=2, expected_revision=page["revision"]
        )
        assert second.output["text"] == "第二行\n尾行"
        assert second.output["end_line"] == 3 and not second.output["truncated"]
        file.write_text("已修改", encoding="utf-8")
        changed = await execute(
            tools, path="中文.py", start_line=2, expected_revision=page["revision"]
        )
        assert changed.error.code == "tool_page_changed"


async def test_list_sorted_pages_and_hide_denied_paths(tmp_path):
    for name in ("b", "a", ".env", ".env.local", "x.key", "private"):
        (tmp_path / name).write_text("夹具")
    (tmp_path / "dir").mkdir()
    (tmp_path / "link").symlink_to("a")
    async with CodingToolRuntime(tmp_path, denied_paths=("private",)) as tools:
        first = await execute(tools, "list_files", limit=2)
        assert [e["name"] for e in first.output["entries"]] == ["a", "b"]
        second = await execute(
            tools, "list_files", offset=2, expected_revision=first.output["revision"]
        )
        assert second.output["entries"] == [
            {"name": "dir", "kind": "directory"},
            {"name": "link", "kind": "symlink"},
        ]
        assert not second.output["truncated"]
        (tmp_path / "c").write_text("新条目")
        changed = await execute(
            tools, "list_files", offset=2, expected_revision=first.output["revision"]
        )
        assert changed.error.code == "tool_page_changed"


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        "a/../b",
        "a//b",
        "a/./b",
        "a\\b",
        "a\x00b",
        ".git/config",
        ".ENV.local",
        ".ssh/id_rsa",
    ],
)
async def test_denied_paths(tmp_path, path):
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, path=path)
        assert result.error.code == "tool_path_denied"
        assert str(tmp_path) not in result.model_dump_json()


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "x", "start_line": True},
        {"path": "x", "max_lines": "2"},
        {"path": "x", "unknown": 1},
        {"path": "x", "start_line": 2},
        {},
    ],
)
async def test_strict_arguments(tmp_path, arguments):
    async with CodingToolRuntime(tmp_path) as tools:
        assert (await execute(tools, **arguments)).error.code == "tool_invalid_arguments"


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "fifo", "directory"])
async def test_reject_links_and_special_files(tmp_path, kind):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").write_text("不可返回的外部内容")
    path = "target"
    if kind == "symlink":
        (root / path).symlink_to(tmp_path / "outside")
    elif kind == "parent_symlink":
        (root / path).symlink_to(tmp_path)
        path += "/outside"
    elif kind == "hardlink":
        os.link(tmp_path / "outside", root / path)
    elif kind == "fifo":
        os.mkfifo(root / path)
    else:
        (root / path).mkdir()
    async with CodingToolRuntime(root) as tools:
        result = await execute(tools, path=path)
        assert result.error.code == (
            "tool_path_denied" if "link" in kind else "tool_wrong_file_type"
        )
        assert "不可返回的外部内容" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xff", "invalid_utf8"),
        (b"a\x00b", "binary_file"),
        (b"\x1b[31m", "binary_file"),
        (b"x" * (MAX_LINE_BYTES + 1), "limit_exceeded"),
    ],
)
async def test_invalid_content_and_long_line(tmp_path, data, code):
    (tmp_path / "x").write_bytes(data)
    async with CodingToolRuntime(tmp_path) as tools:
        assert (await execute(tools, path="x")).error.code == f"tool_{code}"


async def test_empty_missing_wrong_type_and_exact_line_limit(tmp_path):
    (tmp_path / "empty").touch()
    (tmp_path / "dir").mkdir()
    data = b"x" * (MAX_LINE_BYTES - 1) + b"\n"
    (tmp_path / "exact").write_bytes(data)
    async with CodingToolRuntime(tmp_path) as tools:
        empty = await execute(tools, path="empty")
        assert empty.output["text"] == "" and empty.output["end_line"] is None
        assert (await execute(tools, path="absent")).error.code == "tool_not_found"
        assert (await execute(tools, path="dir")).error.code == "tool_wrong_file_type"
        exact = await execute(tools, path="exact", max_lines=1)
        assert exact.output["text"].encode() == data and not exact.output["truncated"]
        end = await execute(
            tools, path="exact", start_line=2, expected_revision=exact.output["revision"]
        )
        assert end.error.code == "tool_offset_out_of_range"


async def test_byte_limit_utf8_and_large_file_prefix(tmp_path):
    line = "中文组合e\u0301" * 50 + "\n"
    (tmp_path / "large").write_bytes(line.encode() * 3000)
    async with CodingToolRuntime(tmp_path) as tools:
        first = await execute(tools, path="large", max_lines=2000)
        assert first.output["utf8_bytes"] <= MAX_TEXT_BYTES
        assert first.output["utf8_bytes"] + len(line.encode()) > MAX_TEXT_BYTES
        assert first.output["text"] == line * first.output["end_line"]
        assert first.output["truncation_reason"] == "byte_limit"
        second = await execute(
            tools,
            path="large",
            start_line=first.output["next_line"],
            expected_revision=first.output["revision"],
            max_lines=1,
        )
        assert second.output["text"] == line


async def test_scan_limit_cannot_be_evaded_by_line_offset(tmp_path):
    (tmp_path / "large").write_bytes((b"x" * 4095 + b"\n") * (MAX_SCAN_BYTES // 4096 + 2))
    async with CodingToolRuntime(tmp_path) as tools:
        first = await execute(tools, path="large", max_lines=1)
        result = await execute(
            tools,
            path="large",
            start_line=MAX_SCAN_BYTES // 4096 + 2,
            expected_revision=first.output["revision"],
        )
        assert result.error.code == "tool_limit_exceeded"


async def test_directory_scan_limit_and_final_json_limit(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        (tmp_path / name).touch()
    async with CodingToolRuntime(tmp_path) as tools:
        monkeypatch.setattr(files, "MAX_DIRECTORY_ENTRIES", 2)
        assert (await execute(tools, "list_files")).error.code == "tool_limit_exceeded"
        monkeypatch.setattr(files, "MAX_DIRECTORY_ENTRIES", 10000)
        import harnessix.tools.runtime as module

        monkeypatch.setattr(module, "MAX_RESULT_BYTES", 10)
        assert (await execute(tools, "list_files")).error.code == "tool_limit_exceeded"


@pytest.mark.parametrize(
    ("number", "code"), [(errno.EACCES, "path_denied"), (errno.EIO, "io_failed")]
)
async def test_os_errors_are_sanitized(tmp_path, monkeypatch, number, code):
    def fail(*args):
        raise OSError(number, "SECRET-CANARY /private/data")

    monkeypatch.setattr(files, "read_file", fail)
    async with CodingToolRuntime(tmp_path) as tools:
        result = await execute(tools, path="x")
        assert result.error.code == f"tool_{code}"
        assert "SECRET-CANARY" not in result.model_dump_json()
