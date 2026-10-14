from __future__ import annotations

import ast
import builtins
import os
import subprocess
import time
import tokenize
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.approvals import execution_fingerprint
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.tools import definition_view, files
from harnessix.tools.contracts import (
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    ReadFileInputV3,
    ReadFileInputV4,
    ReadFileViewOutput,
    ReadToolError,
)
from harnessix.tools.files import _decode
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.tools.test_files import call


def _span(source: str | bytes, line: int = 1):
    content = source.encode() if isinstance(source, str) else source
    operation = ReadOperation()
    lines = definition_view.source_lines(content, operation, _decode)
    return definition_view.definition_span(lines, line, operation)


@pytest.mark.parametrize(
    ("source", "line", "expected"),
    [
        ("def chosen():\n    return 1\n", 1, ("function", 1, 2, 2, None)),
        ("async def chosen():\n    await work()\n", 1, ("function", 1, 2, 2, None)),
        ("class Chosen:\n    value = 1\n", 1, ("class", 1, 2, 2, None)),
        (
            "@decorator\nasync def chosen(\n    arg: dict[str, int],\n) -> int:\n"
            "    return arg['key']\n",
            2,
            ("function", 4, 5, 5, None),
        ),
        (
            "class Chosen(\n    Base,\n    metaclass=Meta,\n):\n    value = 1\n",
            1,
            ("class", 4, 5, 5, None),
        ),
        (
            "class Container:\n    def chosen(self):\n        return 1\n\n"
            "    # lexical trailing comment\n    def next(self):\n        pass\n",
            2,
            ("function", 2, 3, 5, None),
        ),
        (
            "def chosen():\n    def nested():\n        return 1\n    return nested()\n"
            "\n# lexical trailing comment\nnext_value = 2\n",
            1,
            ("function", 1, 2, 6, None),
        ),
        ("def chosen(): return 1\nother = 2\n", 1, ("function", 1, 1, 1, None)),
        (
            "def chosen(): 'inline'; return 1\nother = 2\n",
            1,
            ("function", 1, 1, 1, None),
        ),
        (
            "def chosen(\n    arg,\n): return arg\nother = 2\n",
            1,
            ("function", 3, 3, 3, None),
        ),
        (
            "def chosen(): return (\n    1 + 2\n)\nother = 2\n",
            1,
            ("function", 1, 1, 3, None),
        ),
        ("class Chosen: pass", 1, ("class", 1, 1, 1, None)),
        (
            "async \\\ndef chosen():\n    pass\n",
            1,
            ("function", 2, 3, 3, None),
        ),
        (
            b"\xef\xbb\xbfdef chosen():\r\n    return 1\r\n",
            1,
            ("function", 1, 2, 2, None),
        ),
        (
            "def chosen():\n    value = '''not a doc\nouter = fake\n'''\n    return value\n"
            "other = 2\n",
            1,
            ("function", 1, 2, 5, None),
        ),
    ],
)
def test_lexical_declaration_and_body_ranges(source, line, expected) -> None:
    assert _span(source, line) == expected


@pytest.mark.parametrize("literal", ['"doc"', "'doc'", 'r"doc"', "U'doc'"])
def test_only_standalone_leading_text_literal_is_omitted(literal: str) -> None:
    source = f"def chosen():\n    {literal}\n    return 1\n"
    assert _span(source) == ("function", 1, 3, 3, (2, 2))


@pytest.mark.parametrize(
    "body",
    [
        '    # leading comment\n    "doc"\n',
        '\n    "doc"\n',
        '    "doc" # trailing comment\n',
        '    """doc\n    continued\n    """ # trailing comment\n',
    ],
)
def test_docstring_with_gap_or_same_physical_statement_comment_is_preserved(body: str) -> None:
    _, _, start, _, omitted = _span("def chosen():\n" + body + "    return 1\n")
    assert start == 2 and omitted is None


def test_multiline_docstring_and_doc_only_empty_body() -> None:
    source = 'def chosen():\n    """doc\n    continued\n    """\n    return 1\n'
    assert _span(source) == ("function", 1, 5, 5, (2, 4))
    assert _span('def chosen():\n    """doc\n    continued\n    """') == (
        "function",
        1,
        5,
        4,
        (2, 4),
    )


@pytest.mark.parametrize(
    "expression",
    [
        "'prefix' + call()",
        "'prefix'.format(value)",
        "'prefix' % value",
        "'prefix', value",
        "'prefix'; run()",
        "f'doc{value}'",
        "f'doc'",
        "b'doc'",
        "('doc')",
        "'adjacent' 'literal'",
        "('adjacent'\n        'literal')",
        "value = 'doc'",
        "return 'doc'",
    ],
)
def test_expressions_and_conservative_literal_forms_never_omit_code(expression: str) -> None:
    kind, header_end, start, end, omitted = _span(f"def chosen():\n    {expression}\n    run()\n")
    assert kind == "function" and header_end == 1 and start == 2 and end >= 3
    assert omitted is None


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("# def chosen():\ndef real():\n    pass\n", 1),
        ('text = "def chosen():"\ndef real():\n    pass\n', 1),
        ('text = """\ndef chosen():\n    pass\n"""\ndef real(): pass\n', 2),
        ('text = f"""\ndef chosen():\n"""\ndef real(): pass\n', 2),
        ("def real(\n    chosen,\n):\n    pass\n", 2),
        ("@chosen\ndef real():\n    pass\n", 1),
        ("def real():\n    chosen = 1\n", 2),
        ("async \\\ndef chosen(): pass\n", 2),
        ("value = def chosen():\n    pass\n", 1),
        ("def chosen:\n    pass\n", 1),
        ("class :\n    pass\n", 1),
        ("", 1),
    ],
)
def test_anchor_must_be_actual_declaration_first_physical_line(source: str, line: int) -> None:
    with pytest.raises(ReadToolError) as failed:
        _span(source, line)
    assert failed.value.code == "definition_unavailable"
    assert str(failed.value) == "definition_unavailable"


@pytest.mark.parametrize(
    "suffix", ["value = (\n", "value = 'unterminated\n", 'value = """unterminated\n', "\t bad\n"]
)
def test_lexical_failure_anywhere_in_complete_source_is_sanitized(suffix: str) -> None:
    with pytest.raises(ReadToolError) as failed:
        _span("def chosen():\n    return 1\n" + suffix)
    assert failed.value.code == "definition_unavailable"
    assert "unterminated" not in str(failed.value)


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"def chosen():\n    return 1\n\xff", "invalid_utf8"),
        (b"def chosen():\n    return 1\n\x00", "binary_file"),
        (b"def chosen():\n    # " + b"x" * MAX_LINE_BYTES, "limit_exceeded"),
        (b"def chosen():\n    pass\n" + b"\n" * 100_000, "limit_exceeded"),
        (b"x" * (MAX_SCAN_BYTES + 1), "limit_exceeded"),
    ],
)
def test_full_source_validation_is_not_avoided_by_short_body(content: bytes, code: str) -> None:
    with pytest.raises(ReadToolError) as failed:
        _span(content)
    assert failed.value.code == code


def test_header_byte_limit_and_source_line_limit_boundaries() -> None:
    header = "def chosen(): #" + "x" * (MAX_LINE_BYTES - len("def chosen(): #\n")) + "\n"
    assert len(header.encode()) == MAX_LINE_BYTES
    assert _span(header + "    pass\n") == ("function", 1, 2, 2, None)
    source = "def chosen(\n" + "    value, # " + "x" * 4070 + "\n):\n    pass\n"
    with pytest.raises(ReadToolError) as failed:
        _span(source)
    assert failed.value.code == "limit_exceeded"
    source = "def chosen():\n    pass\n" + "\n" * (100_000 - 2)
    assert _span(source) == ("function", 1, 2, 100_000, None)


@pytest.mark.parametrize("phase", ["source", "token"])
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_source_and_token_checkpoints_propagate_stop(
    monkeypatch: pytest.MonkeyPatch, phase: str, stop: str
) -> None:
    operation = ReadOperation()
    original = operation.checkpoint
    checks = 0

    def stopping_checkpoint() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            if stop == "cancel":
                operation.stopped.set()
            else:
                operation.deadline = time.monotonic() - 1
        original()

    monkeypatch.setattr(operation, "checkpoint", stopping_checkpoint)
    with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as failed:
        if phase == "source":
            definition_view.source_lines(b"def chosen():\n    pass\n\n", operation, _decode)
        else:
            definition_view.definition_span(["def chosen():\n", "    pass\n"], 1, operation)
    if stop == "timeout":
        assert failed.value.code == "timeout"


def test_each_token_has_its_own_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    operation = ReadOperation()
    checks = []
    monkeypatch.setattr(operation, "checkpoint", lambda: checks.append(True))
    expected = [tokenize.TokenInfo(tokenize.NAME, "name", (1, 0), (1, 4), "name\n")] * 8

    def tokens(_readline) -> Iterator[tokenize.TokenInfo]:
        yield from expected

    monkeypatch.setattr(tokenize, "generate_tokens", tokens)
    assert list(definition_view._tokens([], operation)) == expected
    assert len(checks) == len(expected)


def test_tokenizer_uses_checked_source_line_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    operation = ReadOperation()
    checks = []
    monkeypatch.setattr(operation, "checkpoint", lambda: checks.append(True))
    lines = ["def chosen():\n", "    pass\n"]

    def tokens(readline) -> Iterator[tokenize.TokenInfo]:
        for expected in [*lines, ""]:
            before = len(checks)
            assert readline() == expected
            assert len(checks) == before + 1
        return iter(())

    monkeypatch.setattr(tokenize, "generate_tokens", tokens)
    assert list(definition_view._tokens(lines, operation)) == []


def test_no_parser_execution_compilation_or_subprocess_api_is_used() -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("定义投影不得使用语义解析或执行API")

    with pytest.MonkeyPatch.context() as patch:
        for target, name in [
            (ast, "parse"),
            (builtins, "exec"),
            (builtins, "compile"),
            (builtins, "eval"),
            (subprocess, "Popen"),
        ]:
            patch.setattr(target, name, forbidden)
        result = _span('def chosen():\n    "doc"\n    return 1\n')
    assert result == ("function", 1, 3, 3, (2, 2))


@pytest.mark.parametrize("mutation", ["grow", "shrink", "rewrite", "replace"])
def test_capture_rejects_size_stat_or_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    target = tmp_path / "chosen.py"
    original_content = b"def chosen():\n    return 1\n" + b"#comment\n" * 10_000
    target.write_bytes(original_content)
    original_read = os.read
    reads = 0

    def changing_read(fd: int, length: int) -> bytes:
        nonlocal reads
        content = original_read(fd, length)
        reads += 1
        if reads == 1:
            if mutation == "grow":
                with target.open("ab") as stream:
                    stream.write(b"#new\n")
            elif mutation == "shrink":
                target.write_bytes(b"#short\n")
            elif mutation == "rewrite":
                before = target.stat()
                target.write_bytes(original_content.replace(b"return 1", b"return 2"))
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            else:
                replacement = tmp_path / "replacement"
                replacement.write_bytes(original_content)
                os.replace(replacement, target)
        return content

    monkeypatch.setattr(os, "read", changing_read)
    operation = ReadOperation()
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        with workspace.open("chosen.py", operation, directory=False) as fd:
            files._capture_definition_content(fd, os.fstat(fd), operation)
    assert failed.value.code == "workspace_changed"


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_capture_stop_closes_workspace_owned_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    (tmp_path / "chosen.py").write_bytes(b"def chosen():\n    pass\n")
    original_read = os.read
    operation = ReadOperation()
    fds = []

    def stopping_read(fd: int, length: int) -> bytes:
        fds.append(fd)
        content = original_read(fd, length)
        if stop == "cancel":
            operation.stopped.set()
        else:
            operation.deadline = time.monotonic() - 1
        return content

    monkeypatch.setattr(os, "read", stopping_read)
    with Workspace(tmp_path) as workspace:
        with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as failed:
            with workspace.open("chosen.py", operation, directory=False) as fd:
                files._capture_definition_content(fd, os.fstat(fd), operation)
        if stop == "timeout":
            assert failed.value.code == "timeout"
        with pytest.raises(OSError):
            os.fstat(fds[0])


def _snapshot(tmp_path: Path, content: bytes, **arguments) -> ReadFileViewOutput:
    path = arguments.pop("path", "chosen.py")
    (tmp_path / path).write_bytes(content)
    args = ReadFileInputV4(path=path, view="definition", anchor="def chosen", **arguments)
    with Workspace(tmp_path) as workspace:
        result = files.read_file_snapshot(workspace, args, ReadOperation())
    assert isinstance(result, ReadFileViewOutput)
    return result


def test_real_header_body_doc_metadata_and_raw_digest(tmp_path: Path) -> None:
    import hashlib

    content = (
        b"# prefix\r\nasync def chosen(\r\n    value: int,\r\n) -> int:\r\n"
        b'    """doc\r\n    continued\r\n    """\r\n'
        b"    return value\r\n\r\n# tail\r\nother = 1\r\n"
    )
    page = _snapshot(tmp_path, content)
    metadata = page.definition
    assert metadata is not None and metadata.kind == "function"
    assert metadata.header.model_dump() == {
        "start_line": 2,
        "end_line": 4,
        "text": "async def chosen(\r\n    value: int,\r\n) -> int:\r\n",
    }
    assert metadata.omitted_docstring.model_dump() == {"start_line": 5, "end_line": 7}
    assert (metadata.body_start_line, metadata.body_end_line) == (8, 10)
    assert page.text == "    return value\r\n\r\n# tail\r\n"
    assert page.text.encode() == b"".join(content.splitlines(keepends=True)[7:10])
    assert page.content_sha256 == hashlib.sha256(content).hexdigest()
    assert page.file_bytes == len(content) and not page.truncated
    assert (page.start_line, page.end_line) == (8, 10)
    assert page.utf8_bytes == len(page.text.encode())


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_quoted_unicode_separator_is_not_a_physical_header_line(
    tmp_path: Path, separator: str
) -> None:
    header = f'def chosen(value="{separator}"):\n'
    result = _snapshot(tmp_path, (header + "    return value\n").encode())
    assert result.definition.header.text == header
    assert result.definition.header.start_line == result.definition.header.end_line == 1
    assert result.text == "    return value\n"


@pytest.mark.parametrize(
    ("content", "start", "end", "text"),
    [
        (
            b'def chosen(): "inline"; return 1\nother = 2\n',
            1,
            1,
            'def chosen(): "inline"; return 1\n',
        ),
        (b"def chosen(\n    value,\n): return value\nother = 2\n", 3, 3, "): return value\n"),
        (b'def chosen():\n    "doc"', 3, 2, ""),
    ],
)
def test_inline_and_doc_only_snapshots(tmp_path: Path, content, start, end, text) -> None:
    result = _snapshot(tmp_path, content)
    assert result.text == text and not result.truncated and result.next_line is None
    assert (result.definition.body_start_line, result.definition.body_end_line) == (start, end)
    assert result.start_line == start
    assert result.end_line == (end if text else None)
    if start == 3 and not text:
        assert result.definition.omitted_docstring.start_line == 2
    else:
        assert result.definition.omitted_docstring is None


@pytest.mark.parametrize("limit", ["line", "byte"])
def test_definition_pagination_is_continuous_and_resumes_with_text(
    tmp_path: Path, limit: str
) -> None:
    content = ('def chosen():\n    "doc"\n' + ("    # 中文\n" * 600) + "    return 1\n").encode()
    target = tmp_path / "chosen.py"
    target.write_bytes(content)
    arguments = (
        {"max_lines": 2}
        if limit == "line"
        else {"max_lines": 2000, "max_utf8_bytes": MAX_LINE_BYTES}
    )
    with Workspace(tmp_path) as workspace:
        first = files.read_file_snapshot(
            workspace,
            ReadFileInputV4(path="chosen.py", view="definition", anchor="def chosen", **arguments),
            ReadOperation(),
        )
        tail = files.read_file_snapshot(
            workspace,
            ReadFileInputV4(
                path="chosen.py",
                view="text",
                start_line=first.next_line,
                max_lines=2000,
                expected_revision=first.revision,
            ),
            ReadOperation(),
        )
    assert first.truncated and first.truncation_reason == f"{limit}_limit"
    assert first.next_line == first.end_line + 1
    assert first.text.encode() == b"".join(content.splitlines(keepends=True)[2 : first.end_line])
    assert first.definition.body_end_line == 603
    assert first.content_sha256 == tail.content_sha256
    assert first.text + tail.text == b"".join(content.splitlines(keepends=True)[2:]).decode()


@pytest.mark.parametrize("anchor", [None, "def chosen"])
@pytest.mark.parametrize("explicit", [False, True])
def test_default_and_explicit_text_preserve_existing_snapshot_exactly(
    tmp_path: Path, anchor: str | None, explicit: bool
) -> None:
    content = b"\xef\xbb\xbfdef chosen():\r\n    return 1\r\n"
    target = tmp_path / "chosen.py"
    target.write_bytes(content)
    arguments = {"path": "chosen.py", "anchor": anchor, "max_lines": 1}
    with Workspace(tmp_path) as workspace:
        old = files.read_file_snapshot(workspace, ReadFileInputV3(**arguments), ReadOperation())
        new = files.read_file_snapshot(
            workspace,
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
        (b"def chosen(chosen):\n    pass\n", "chosen.py", "chosen", "anchor_ambiguous"),
        (
            b"# def chosen():\ndef real(): pass\n",
            "chosen.py",
            "def chosen",
            "definition_unavailable",
        ),
        (b'var = """\ndef chosen():\n"""\n', "chosen.py", "def chosen", "definition_unavailable"),
        (
            b"def real(\n    chosen,\n):\n    pass\n",
            "chosen.py",
            "chosen",
            "definition_unavailable",
        ),
        (
            b"def chosen():\n    return 1\nvalue = (\n",
            "chosen.py",
            "def chosen",
            "definition_unavailable",
        ),
        (b"def chosen():\n    return 1\n\xff", "chosen.py", "def chosen", "invalid_utf8"),
    ],
)
def test_posix_definition_errors_are_fixed_codes(tmp_path, content, path, anchor, code) -> None:
    (tmp_path / path).write_bytes(content)
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        files.read_file_snapshot(
            workspace, ReadFileInputV4(path=path, view="definition", anchor=anchor), ReadOperation()
        )
    assert failed.value.code == code and str(failed.value) == code


def test_pyi_and_class_snapshot_keep_real_header(tmp_path: Path) -> None:
    (tmp_path / "chosen.pyi").write_bytes(b'class Chosen:\n    "doc"\n    value: int\n')
    with Workspace(tmp_path) as workspace:
        result = files.read_file_snapshot(
            workspace,
            ReadFileInputV4(path="chosen.pyi", view="definition", anchor="class Chosen"),
            ReadOperation(),
        )
    assert result.definition.kind == "class" and result.definition.header.text == "class Chosen:\n"
    assert result.text == "    value: int\n"


def test_posix_digest_and_projection_use_single_capture_without_reseek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    content = b"\xef\xbb\xbfdef chosen():\r\n    return 1\r\n"
    (tmp_path / "chosen.py").write_bytes(content)
    original_open, original_read = os.open, os.read
    target_fds, captured = [], []

    def track_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if path == "chosen.py":
            target_fds.append(fd)
        return fd

    def track_read(fd, count):
        assert fd in target_fds
        raw = original_read(fd, count)
        captured.append(raw)
        return raw

    def forbidden(*_args):
        pytest.fail("定义视图不得重读、重定位或独立扫描摘要")

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "read", track_read)
    monkeypatch.setattr(os, "lseek", forbidden)
    monkeypatch.setattr(files, "_complete_content_digest", forbidden)
    monkeypatch.setattr(files, "_read_file_page", forbidden)
    with Workspace(tmp_path) as workspace:
        result = files.read_file_snapshot(
            workspace,
            ReadFileInputV4(path="chosen.py", view="definition", anchor="def chosen"),
            ReadOperation(),
        )
    assert len(target_fds) == 1 and b"".join(captured) == content
    assert result.content_sha256 == hashlib.sha256(b"".join(captured)).hexdigest()
    assert result.definition.header.text.encode() == content.splitlines(keepends=True)[0]


@pytest.mark.parametrize("mutation", ["rewrite", "replace"])
def test_change_after_projection_is_not_published(tmp_path, monkeypatch, mutation) -> None:
    target = tmp_path / "chosen.py"
    target.write_bytes(b"def chosen():\n    return 1\n")
    original = files.definition_page

    def changing_projection(*arguments):
        result = original(*arguments)
        if mutation == "rewrite":
            target.write_bytes(b"def chosen():\n    return 2\n")
        else:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"def chosen():\n    return 2\n")
            os.replace(replacement, target)
        return result

    monkeypatch.setattr(files, "definition_page", changing_projection)
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        files.read_file_snapshot(
            workspace,
            ReadFileInputV4(path="chosen.py", view="definition", anchor="def chosen"),
            ReadOperation(),
        )
    assert failed.value.code == "workspace_changed"


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "directory", "fifo"])
def test_definition_cannot_bypass_links_or_file_type(tmp_path, kind) -> None:
    original = tmp_path / "original.py"
    original.write_bytes(b"def chosen():\n    pass\n")
    root = tmp_path / "repo"
    root.mkdir()
    path = "chosen.py"
    if kind == "symlink":
        (root / path).symlink_to(original)
    elif kind == "parent_symlink":
        (root / "parent").symlink_to(tmp_path)
        path = "parent/original.py"
    elif kind == "hardlink":
        os.link(original, root / path)
    elif kind == "directory":
        (root / path).mkdir()
    else:
        os.mkfifo(root / path)
    with Workspace(root) as workspace, pytest.raises(ReadToolError) as failed:
        files.read_file_snapshot(
            workspace,
            ReadFileInputV4(path=path, view="definition", anchor="def chosen"),
            ReadOperation(),
        )
    assert failed.value.code == ("path_denied" if "link" in kind else "wrong_file_type")


def test_expected_revision_failure_precedes_capture(tmp_path, monkeypatch) -> None:
    (tmp_path / "chosen.py").write_bytes(b"def chosen():\n    pass\n")
    monkeypatch.setattr(
        files, "_capture_definition_content", lambda *_args: pytest.fail("不得捕获")
    )
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        files.read_file_snapshot(
            workspace,
            ReadFileInputV4(
                path="chosen.py", view="definition", anchor="def chosen", expected_revision="0" * 64
            ),
            ReadOperation(),
        )
    assert failed.value.code == "page_changed"


@pytest.mark.parametrize("kind", ["workspace", "fingerprint", "call", "thread", "turn"])
async def test_definition_scope_owner_mismatch_precedes_target_io(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(files, "read_file_snapshot", lambda *_args: pytest.fail("归属失败不得I/O"))
    async with CodingToolRuntime(tmp_path, enable_definition_views=True) as tools:
        request = call(tools, path="chosen.py", view="definition", anchor="def chosen")
        thread_id, turn_id = uuid4(), uuid4()
        workspace = str(tools.workspace_root)
        fingerprint = execution_fingerprint(thread_id, turn_id, workspace, request)
        scope = ToolExecutionScope(
            uuid4() if kind == "thread" else thread_id,
            uuid4() if kind == "turn" else turn_id,
            uuid4() if kind == "call" else request.call_id,
            workspace + "/other" if kind == "workspace" else workspace,
            "0" * 64 if kind == "fingerprint" else fingerprint,
        )
        with pytest.raises(KernelError) as failed:
            await tools.execute_scoped(request, scope, CancelToken())
    assert failed.value.code in {"tool_workspace_mismatch", "tool_scope_mismatch"}
