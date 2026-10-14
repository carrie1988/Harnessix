"""泛化合成源码的RE2匹配、原字节摘要及跨平台有界失败。"""

from __future__ import annotations

import hashlib
import os
import time
from importlib import metadata

import pytest
import re2
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.tools import grep_matcher, search, windows_search
from harnessix.tools.contracts import MAX_LINE_BYTES, READ_TIMEOUT_SECONDS, ReadToolError
from harnessix.tools.search_contracts import (
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_TOTAL_BYTES,
    GrepSnapshotInput,
    GrepSnapshotMatch,
    GrepSnapshotOutput,
    RegexGrepInput,
)
from harnessix.tools.windows_read_port import WindowsReadPort
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.tools.test_windows_grep_snapshots import _MemoryWindowsRoot


@pytest.fixture(params=["posix", "windows"])
def run_grep(tmp_path, request):
    """POSIX读真实临时文件；Windows复用既有内存Observation端口。"""
    if request.param == "posix" and os.name != "posix":
        pytest.skip("POSIX读取端")

    def invoke(args, files, *, operation=None, capture=None):
        operation = operation or ReadOperation()
        if request.param == "posix":
            for path, body in files.items():
                target = tmp_path / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            with Workspace(tmp_path) as workspace:
                return search.grep(workspace, args, operation, capture=capture)
        root = _MemoryWindowsRoot(tmp_path)
        root.files = files
        root.ignored.clear()
        port = WindowsReadPort(tmp_path, denied_paths=(), root_factory=lambda _: root)
        try:
            output = windows_search.grep_files(port, args, operation, capture=capture)
            reads = [path for path, content, _ in root.calls if content]
            assert len(reads) == len(set(reads)) == output.stats.files_read
            return output
        finally:
            port.close()

    return invoke


@pytest.mark.parametrize(
    "query,lines",
    [
        (r"^class\s+\w+\s*\(", [1, 2]),
        (r"class\s+(SyntheticNode|OtherNode)\s*\(", [1, 2]),
        (r"class\s+SyntheticNode\s*\(", [1]),
        (r"^def\s+compute\s*\(", [3]),
        (r"A\+B\[0\]\.value", [4]),
        (r"不存在", []),
    ],
)
def test_synthetic_symbols_whitespace_alternation_and_escaped_meta(run_grep, query, lines):
    body = (
        b"class SyntheticNode(Base):\nclass  OtherNode (Base):\n"
        b"def compute (value):\nA+B[0].value\nclass third:\n"
    )
    output = run_grep(RegexGrepInput(query=query), {"source.py": body})
    assert type(output) is GrepSnapshotOutput
    assert [hit.line for hit in output.matches] == lines
    assert output.scan_complete
    for hit in output.matches:
        assert type(hit) is GrepSnapshotMatch
        assert hit.content_sha256 == hashlib.sha256(body).hexdigest()
        assert hit.file_bytes == len(body)


@pytest.mark.parametrize("query", ["a+b[0].value", "(?=secret)", "(unclosed"])
def test_explicit_literal_does_not_compile_or_rewrite(run_grep, monkeypatch, query):
    monkeypatch.setattr(re2, "compile", lambda *a, **kw: pytest.fail("literal不得编译"))
    body = f"prefix {query} suffix\nnot the literal\n".encode()
    output = run_grep(RegexGrepInput(query=query, match_mode="literal"), {"source.py": body})
    assert [hit.line for hit in output.matches] == [1]
    assert query in output.matches[0].text


@pytest.mark.parametrize(
    "query,lines", [("École", [1]), ("école", [2]), ("(?i)école", [1, 2]), ("中文", [1, 2])]
)
def test_unicode_case_is_explicit_and_not_normalized(run_grep, query, lines):
    body = "École 中文\nécole 中文\nE\u0301cole\n".encode()
    output = run_grep(RegexGrepInput(query=query), {"source.py": body})
    assert [hit.line for hit in output.matches] == lines


@pytest.mark.parametrize("query,lines", [("^", [1, 2, 3]), ("^$", [2]), ("z+", [])])
def test_empty_matches_return_one_record_per_line_and_no_hits_are_complete(run_grep, query, lines):
    output = run_grep(RegexGrepInput(query=query), {"source.py": b"a\n\nb\n"})
    assert [hit.line for hit in output.matches] == lines
    assert output.scan_complete and output.stats.files_read == 1


def test_bom_crlf_sha_and_single_content_observation(run_grep, monkeypatch):
    body = "\ufeffpreamble\r\nclass SyntheticNode (Base):\r\n尾行\r\n".encode()
    reads = []
    original = search._read

    def count_read(fd, operation, scan):
        reads.append(fd)
        return original(fd, operation, scan)

    monkeypatch.setattr(search, "_read", count_read)
    output = run_grep(RegexGrepInput(query=r"^class\s+\w+\s*\("), {"source.py": body})
    hit = output.matches[0]
    assert len(reads) <= 1  # Windows的单次内容观察由run_grep断言。
    assert hit.content_sha256 == hashlib.sha256(body).hexdigest()
    assert hit.file_bytes == len(body)
    assert hit.line == 2 and hit.text == "class SyntheticNode (Base):"
    assert [line.text for line in hit.context.lines] == [
        "\ufeffpreamble",
        "class SyntheticNode (Base):",
        "尾行",
    ]


def test_compile_once_per_call_across_files_and_public_options(run_grep, monkeypatch):
    compiled = []
    original = re2.compile

    def compile_once(query, *, options):
        compiled.append(options)
        return original(query, options=options)

    monkeypatch.setattr(re2, "compile", compile_once)
    files = {"a.py": b"class SyntheticNode(Base):\n", "b.py": b"class OtherNode (Base):\n"}
    output = run_grep(RegexGrepInput(query=r"^class\s+\w+\s*\("), files)
    assert len(output.matches) == 2 and len(compiled) == 1
    options = compiled[0]
    assert options.max_mem == 1024 * 1024
    assert options.log_errors is False and options.case_sensitive is True
    assert options.never_capture is True


@pytest.mark.parametrize(
    "query",
    [
        "(PRIVATE_MARKER",
        r"(PRIVATE_MARKER)\1",
        "(?=PRIVATE_MARKER)",
        "(?<=PRIVATE_MARKER)x",
        "(?!PRIVATE_MARKER)",
        "(?<!PRIVATE_MARKER)x",
    ],
)
def test_invalid_and_unsupported_regex_fail_without_pattern_or_stderr(run_grep, query, capfd):
    with pytest.raises(grep_matcher.RegexSearchError) as raised:
        run_grep(RegexGrepInput(query=query), {})
    error = raised.value
    assert error.code == "tool_regex_invalid"
    assert "match_mode=literal" in error.message and "RE2" in error.message
    assert "PRIVATE_MARKER" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert capfd.readouterr().err == ""


@pytest.mark.parametrize("failure", ["missing-package", "version-drift", "missing-library"])
def test_missing_or_drifted_engine_fails_closed_but_literal_still_works(
    run_grep, monkeypatch, failure
):
    if failure == "missing-package":

        def missing(_):
            raise metadata.PackageNotFoundError("PRIVATE_MARKER")

        monkeypatch.setattr(grep_matcher.metadata, "version", missing)
    elif failure == "version-drift":
        monkeypatch.setattr(grep_matcher.metadata, "version", lambda _: "9.9.9")
    else:

        def missing(_):
            raise ImportError("PRIVATE_MARKER")

        monkeypatch.setattr(grep_matcher.importlib, "import_module", missing)
    with pytest.raises(grep_matcher.RegexSearchError) as raised:
        run_grep(RegexGrepInput(query="PRIVATE_MARKER"), {})
    assert raised.value.code == "tool_regex_unavailable"
    assert "PRIVATE_MARKER" not in str(raised.value)
    output = run_grep(
        RegexGrepInput(query="needle", match_mode="literal"), {"source.py": b"needle\n"}
    )
    assert len(output.matches) == 1


@pytest.mark.parametrize("query", ["(a+)+$", "(a|aa)+$"])
def test_backtracking_evil_patterns_finish_inside_existing_scan_budget(run_grep, query):
    # 每行低于原4 KiB上限，总量接近2 MiB；不靠跳过长行掩盖回溯风险。
    body = (b"a" * 4000 + b"!\n") * 450
    started = time.monotonic()
    output = run_grep(RegexGrepInput(query=query), {"source.py": body})
    assert time.monotonic() - started < READ_TIMEOUT_SECONDS
    assert not output.matches and output.scan_complete
    assert output.stats.bytes_read == len(body) and output.stats.long_lines == 0


@pytest.mark.parametrize("when", ["before", "after"])
def test_every_matched_line_checks_cancellation_before_and_after(when):
    operation = ReadOperation()
    args = RegexGrepInput(query="needle", context_lines=0)
    actual_matcher = grep_matcher.build_line_matcher(args, operation)
    calls = []

    def matcher(line):
        calls.append(line)
        index = actual_matcher(line)
        operation.stopped.set()
        return index

    if when == "before":
        operation.stopped.set()
    with pytest.raises(TurnCancelled):
        list(
            search.grep_line_matches(
                "source.py", "a" * 64, "needle\n", args, operation, search._Scan(), matcher=matcher
            )
        )
    assert len(calls) == (0 if when == "before" else 1)


def test_matching_checkpoint_enforces_scan_deadline():
    operation = ReadOperation()

    def matcher(_):
        operation.deadline = time.monotonic() - 1
        return 0

    with pytest.raises(ReadToolError) as raised:
        list(
            search.grep_line_matches(
                "source.py",
                "a" * 64,
                "needle\n",
                RegexGrepInput(query="needle"),
                operation,
                search._Scan(),
                matcher=matcher,
            )
        )
    assert raised.value.code == "timeout"


@pytest.mark.parametrize(
    "invalid",
    [
        {"query": ""},
        {"query": "a" * 257},
        {"query": "界" * 86},
        {"query": "secret\n"},
        {"query": "secret\x00"},
        {"match_mode": "REGEX"},
        {"match_mode": None},
        {"context_lines": 4},
        {"context_lines": True},
        {"max_results": 201},
        {"include": "../*"},
    ],
)
def test_inherited_input_bounds_remain_strict(invalid):
    with pytest.raises(ValidationError):
        RegexGrepInput.model_validate({"query": "needle", **invalid})
    args = RegexGrepInput(query="界" * 85)
    assert (args.match_mode, args.context_lines, args.max_results) == ("regex", 2, 10)
    assert MAX_SEARCH_FILE_BYTES == 2 * 1024 * 1024
    assert MAX_SEARCH_TOTAL_BYTES == 16 * 1024 * 1024


def test_regex_and_literal_inherit_context_preview_and_artifact_limits(run_grep):
    body = (b"needle" + b"\t" * 378 + b"\n") * 40
    capture = search.SearchCapture()
    output = run_grep(
        RegexGrepInput(query="n(eed|xxx)le", max_results=200, context_lines=3),
        {"source.py": body},
        capture=capture,
    )
    assert output.truncated and output.truncation_reason == "output_limit"
    assert sum(len(hit.model_dump_json().encode()) + 1 for hit in output.matches) <= 24 * 1024
    assert capture.complete and capture.records == 40
    hits = [GrepSnapshotMatch.model_validate_json(line) for line in capture.body.splitlines()]
    assert all(hit.content_sha256 == hashlib.sha256(body).hexdigest() for hit in hits)
    assert all(len(hit.context.lines) <= 7 for hit in hits)


def test_oversize_long_binary_and_invalid_utf8_files_are_gaps(run_grep):
    files = {
        "a.py": b"needle\n",
        "binary.py": b"needle\x00",
        "invalid.py": b"needle\xff",
        "long.py": b"needle" + b"x" * MAX_LINE_BYTES + b"\n",
        "oversize.py": b"x" * (MAX_SEARCH_FILE_BYTES + 1),
    }
    output = run_grep(RegexGrepInput(query="needle"), files)
    assert [hit.path for hit in output.matches] == ["a.py"]
    assert not output.scan_complete and not output.truncated
    assert (
        output.stats.binary_files,
        output.stats.invalid_utf8_files,
        output.stats.long_lines,
        output.stats.oversized_files,
    ) == (1, 1, 1, 1)


def test_total_scan_budget_is_not_bypassed(run_grep, monkeypatch):
    monkeypatch.setattr(search, "MAX_SEARCH_TOTAL_BYTES", 16)
    monkeypatch.setattr(windows_search, "MAX_SEARCH_TOTAL_BYTES", 16)
    with pytest.raises(ReadToolError) as raised:
        run_grep(
            RegexGrepInput(query="needle"),
            {"a.py": b"needle\n", "b.py": b"needle\n", "c.py": b"needle\n"},
        )
    assert raised.value.code == "limit_exceeded"


def test_legacy_snapshot_literal_behavior_is_not_regex(run_grep):
    output = run_grep(GrepSnapshotInput(query="a+b"), {"source.py": b"aaab\na+b\n"})
    assert [hit.line for hit in output.matches] == [2]
