"""Windows内存观察端口验收；不代表原生Windows发行验收。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.tools import windows_file_read
from harnessix.tools.contracts import (
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    ReadFileInputV3,
    ReadFileInputV4,
    ReadFileViewOutput,
    ReadToolError,
)
from harnessix.tools.windows_read_port import WindowsReadPort
from harnessix.tools.workspace import ReadOperation
from harnessix.workspace.contracts import ResourceAccess


@dataclass
class _Observed:
    kind: Literal["file", "directory", "missing"] = "file"
    identity: tuple[object, ...] = ("memory-object", 1)
    content: bytes | None = b""
    size: int = 0
    entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...] | None = (
        None
    )


class _MemoryRoot:
    def __init__(self, path: Path, content: bytes) -> None:
        self.path = path
        self.root_identity: tuple[object, ...] = ("memory-root",)
        self.observed = _Observed(content=content, size=len(content))
        self.error: str | None = None
        self.calls: list[tuple[str, ResourceAccess, bool, int]] = []

    def observe(
        self,
        path: str,
        *,
        access: ResourceAccess,
        include_content: bool,
        max_bytes: int,
        checkpoint: Callable[[], None] | None,
    ) -> _Observed:
        self.calls.append((path, access, include_content, max_bytes))
        assert access == "read" and include_content and checkpoint is not None
        checkpoint()
        if self.error:
            raise KernelError(self.error, "private canary must not be echoed")
        if self.observed.content is not None and len(self.observed.content) > max_bytes:
            raise KernelError("workspace_snapshot_limit", "private canary")
        return self.observed

    def close(self) -> None:
        pass


def _port(tmp_path: Path, content: bytes) -> tuple[WindowsReadPort, _MemoryRoot]:
    root = _MemoryRoot(tmp_path, content)
    port = WindowsReadPort(tmp_path, denied_paths=("private",), root_factory=lambda _path: root)
    return port, root


def _read(port, path="chosen.py", anchor="def chosen", **arguments) -> ReadFileViewOutput:
    args = ReadFileInputV4(path=path, view="definition", anchor=anchor, **arguments)
    result = windows_file_read.read_file_snapshot(port, args, ReadOperation())
    assert isinstance(result, ReadFileViewOutput)
    return result


@pytest.mark.parametrize(
    ("source", "path", "anchor", "header", "text", "omitted"),
    [
        (
            'def chosen():\n    "doc"\n    return 1\nother = 2\n',
            "chosen.py",
            "def chosen",
            "def chosen():\n",
            "    return 1\n",
            (2, 2),
        ),
        (
            'async def chosen(\n    value,\n):\n    "doc"\n    return value\n',
            "chosen.py",
            "async def chosen",
            "async def chosen(\n    value,\n):\n",
            "    return value\n",
            (4, 4),
        ),
        (
            'class Chosen:\n    "doc"\n    value: int\n',
            "chosen.pyi",
            "class Chosen",
            "class Chosen:\n",
            "    value: int\n",
            (2, 2),
        ),
        (
            'def chosen(): "inline"; return 1\n',
            "chosen.py",
            "def chosen",
            'def chosen(): "inline"; return 1\n',
            'def chosen(): "inline"; return 1\n',
            None,
        ),
        ('def chosen():\n    "doc"', "chosen.py", "def chosen", "def chosen():\n", "", (2, 2)),
        (
            'def chosen():\n    "prefix" + call()\n',
            "chosen.py",
            "def chosen",
            "def chosen():\n",
            '    "prefix" + call()\n',
            None,
        ),
        (
            'def chosen():\n    f"value{value}"\n',
            "chosen.py",
            "def chosen",
            "def chosen():\n",
            '    f"value{value}"\n',
            None,
        ),
        (
            'def chosen():\n    # keep\n    "doc"\n    return 1\n',
            "chosen.py",
            "def chosen",
            "def chosen():\n",
            '    # keep\n    "doc"\n    return 1\n',
            None,
        ),
        (
            'def chosen():\n\n    "doc"\n    return 1\n',
            "chosen.py",
            "def chosen",
            "def chosen():\n",
            '\n    "doc"\n    return 1\n',
            None,
        ),
        (
            'def chosen():\n    "doc" # keep\n    return 1\n',
            "chosen.py",
            "def chosen",
            "def chosen():\n",
            '    "doc" # keep\n    return 1\n',
            None,
        ),
        (
            "\ufeffdef chosen():\r\n    return 1\r\n",
            "chosen.py",
            "def chosen",
            "\ufeffdef chosen():\r\n",
            "    return 1\r\n",
            None,
        ),
        (
            'def chosen(value="\u2028"):\n    return value\n',
            "chosen.py",
            "def chosen",
            'def chosen(value="\u2028"):\n',
            "    return value\n",
            None,
        ),
    ],
)
def test_shared_projection_keeps_same_observation_bytes_and_ranges(
    tmp_path, source, path, anchor, header, text, omitted
) -> None:
    content = source.encode()
    port, root = _port(tmp_path, content)
    page = _read(port, path, anchor)
    assert root.calls == [(path, "read", True, MAX_SCAN_BYTES)]
    assert page.content_sha256 == hashlib.sha256(content).hexdigest()
    assert page.file_bytes == len(content) and page.digest_status == "complete"
    assert page.text == text and page.definition.header.text == header
    assert not page.truncated and page.next_line is None
    lines = content.split(b"\n")
    assert page.definition.header.start_line == 1
    assert page.definition.header.end_line == header.count("\n") + int(not header.endswith("\n"))
    if omitted:
        assert (
            page.definition.omitted_docstring.start_line,
            page.definition.omitted_docstring.end_line,
        ) == omitted
    else:
        assert page.definition.omitted_docstring is None
    if text:
        expected = b"\n".join(lines[page.start_line - 1 : page.end_line])
        if text.endswith("\n"):
            expected += b"\n"
        assert page.text.encode() == expected


@pytest.mark.parametrize("limit", ["line", "byte"])
def test_windows_definition_pagination_continues_as_text(tmp_path, limit) -> None:
    content = ('def chosen():\n    "doc"\n' + "    # 中文\n" * 600 + "    return 1\n").encode()
    port, _root = _port(tmp_path, content)
    arguments = (
        {"max_lines": 2}
        if limit == "line"
        else {"max_lines": 2000, "max_utf8_bytes": MAX_LINE_BYTES}
    )
    first = _read(port, **arguments)
    tail = windows_file_read.read_file_snapshot(
        port,
        ReadFileInputV4(
            path="chosen.py",
            start_line=first.next_line,
            max_lines=2000,
            expected_revision=first.revision,
        ),
        ReadOperation(),
    )
    assert first.truncated and first.truncation_reason == f"{limit}_limit"
    assert first.next_line == first.end_line + 1
    assert first.text + tail.text == b"\n".join(content.split(b"\n")[2:]).decode()
    assert first.content_sha256 == tail.content_sha256


@pytest.mark.parametrize("anchor", [None, "def chosen"])
@pytest.mark.parametrize("explicit", [False, True])
def test_windows_text_default_and_explicit_preserve_old_bytes(tmp_path, anchor, explicit) -> None:
    content = b"\xef\xbb\xbfdef chosen():\r\n    return 1\r\n"
    port, _root = _port(tmp_path, content)
    arguments = {"path": "chosen.py", "anchor": anchor, "max_lines": 1}
    old = windows_file_read.read_file_snapshot(port, ReadFileInputV3(**arguments), ReadOperation())
    new = windows_file_read.read_file_snapshot(
        port,
        ReadFileInputV4(**arguments, **({"view": "text"} if explicit else {})),
        ReadOperation(),
    )
    assert old.model_dump() == new.model_dump()
    assert new.text.encode() == content.splitlines(keepends=True)[0]


@pytest.mark.parametrize(
    ("content", "path", "anchor", "code"),
    [
        (b"def chosen():\n    pass\n", "chosen.js", "def chosen", "definition_unavailable"),
        (b"def chosen():\n    pass\n", "chosen.py", "absent", "anchor_not_found"),
        (
            b"def chosen():\n    pass\ndef chosen(): pass\n",
            "chosen.py",
            "def chosen",
            "anchor_ambiguous",
        ),
        (
            b"# def chosen():\ndef real(): pass\n",
            "chosen.py",
            "def chosen",
            "definition_unavailable",
        ),
        (b'var = """\ndef chosen():\n"""\n', "chosen.py", "def chosen", "definition_unavailable"),
        (
            b"def chosen():\n    return 1\nvalue = (\n",
            "chosen.py",
            "def chosen",
            "definition_unavailable",
        ),
        (b"def chosen():\n    return 1\n\xff", "chosen.py", "def chosen", "invalid_utf8"),
        (b"def chosen():\n    return 1\n\x00", "chosen.py", "def chosen", "binary_file"),
        (
            b"def chosen():\n    pass\n" + b"\n" * 100_000,
            "chosen.py",
            "def chosen",
            "limit_exceeded",
        ),
        (
            b"def chosen():\n    #" + b"x" * MAX_LINE_BYTES,
            "chosen.py",
            "def chosen",
            "limit_exceeded",
        ),
        (b"x" * (MAX_SCAN_BYTES + 1), "chosen.py", "def chosen", "limit_exceeded"),
    ],
)
def test_windows_errors_are_fixed_without_source_or_exception_text(
    tmp_path, content, path, anchor, code
) -> None:
    port, _root = _port(tmp_path, content)
    with pytest.raises(ReadToolError) as failed:
        _read(port, path, anchor)
    assert failed.value.code == code and str(failed.value) == code


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_native_type_validation_precedes_shared_projection(tmp_path, monkeypatch, kind) -> None:
    port, root = _port(tmp_path, b"def chosen():\n    pass\n")
    root.observed = replace(root.observed, kind=kind)
    monkeypatch.setattr(
        windows_file_read, "definition_page", lambda *_args: pytest.fail("不得投影")
    )
    with pytest.raises(ReadToolError) as failed:
        _read(port)
    assert failed.value.code == ("not_found" if kind == "missing" else "wrong_file_type")


@pytest.mark.parametrize("invalid", ["missing_content", "size"])
def test_native_content_size_mismatch_cannot_be_published(tmp_path, invalid) -> None:
    port, root = _port(tmp_path, b"def chosen():\n    pass\n")
    root.observed = (
        replace(root.observed, content=None)
        if invalid == "missing_content"
        else replace(root.observed, size=root.observed.size + 1)
    )
    with pytest.raises(ReadToolError) as failed:
        _read(port)
    assert failed.value.code == "io_failed"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("workspace_path_denied", "path_denied"),
        ("workspace_changed", "workspace_changed"),
        ("workspace_wrong_file_type", "wrong_file_type"),
        ("workspace_snapshot_limit", "limit_exceeded"),
        ("workspace_parent_missing", "not_found"),
    ],
)
def test_native_authorization_and_observation_errors_are_not_weakened(
    tmp_path, monkeypatch, error, code
) -> None:
    port, root = _port(tmp_path, b"def chosen():\n    pass\n")
    root.error = error
    monkeypatch.setattr(
        windows_file_read, "definition_page", lambda *_args: pytest.fail("不得投影")
    )
    with pytest.raises(ReadToolError) as failed:
        _read(port)
    assert str(failed.value) == code and root.calls[0][1] == "read"


@pytest.mark.parametrize("path", ["../chosen.py", "private/chosen.py", ".env.py", "a\\chosen.py"])
def test_denied_path_never_reaches_native_observe(tmp_path, path) -> None:
    port, root = _port(tmp_path, b"def chosen():\n    pass\n")
    with pytest.raises(ReadToolError) as failed:
        _read(port, path)
    assert failed.value.code == "path_denied" and not root.calls


def test_windows_stale_revision_never_projects_source(tmp_path, monkeypatch) -> None:
    port, _root = _port(tmp_path, b"def chosen():\n    pass\n")
    monkeypatch.setattr(
        windows_file_read, "definition_page", lambda *_args: pytest.fail("不得投影")
    )
    with pytest.raises(ReadToolError) as failed:
        _read(port, expected_revision="0" * 64)
    assert failed.value.code == "page_changed"


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_windows_cancel_and_original_deadline_fail_without_partial_output(tmp_path, stop) -> None:
    port, root = _port(tmp_path, b"def chosen():\n    pass\n")
    operation = ReadOperation()
    assert 4.8 < operation.deadline - time.monotonic() <= 5
    if stop == "cancel":
        operation.stopped.set()
    else:
        operation.deadline = time.monotonic() - 1
    with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as failed:
        windows_file_read.read_file_snapshot(
            port,
            ReadFileInputV4(path="chosen.py", view="definition", anchor="def chosen"),
            operation,
        )
    if stop == "timeout":
        assert failed.value.code == "timeout"
    assert not root.calls
