"""Windows内存观察端口：完整原字节摘要及不稳定/缺口负控。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.tools import search, windows_search
from harnessix.tools.contracts import MAX_LINE_BYTES, ReadToolError
from harnessix.tools.search_contracts import (
    MAX_SEARCH_FILE_BYTES,
    GrepInput,
    GrepMatch,
    GrepOutput,
    GrepSnapshotInput,
    GrepSnapshotMatch,
    GrepSnapshotOutput,
)
from harnessix.tools.windows_read_port import WindowsReadPort, file_revision
from harnessix.tools.workspace import ReadOperation
from tests.tools.test_grep_snapshots import LEGACY_BODY, LEGACY_GREP_JSON
from tests.tools.test_windows_read_adapter import _Observed


class _MemoryWindowsRoot:
    """沿用既有_Observed范式；只返回内存内容，不读取宿主文件。"""

    def __init__(self, path):
        self.path = path
        self.root_identity = ("memory-root",)
        self.files = {"source.py": LEGACY_BODY}
        self.ignored = {"link.py": "symlink", "hardlink.py": "special"}
        self.unreadable = set()
        self.calls = []
        self.closed = False

    def observe(self, path, *, access, include_content, max_bytes, checkpoint):
        assert access == "read"
        self.calls.append((path, include_content, max_bytes))
        checkpoint()
        if path in self.unreadable:
            raise KernelError("memory_io_failed", "unreadable")
        if path in self.files:
            body = self.files[path]
            if include_content and len(body) > max_bytes:
                raise KernelError("workspace_snapshot_limit", "limit")
            return _Observed(
                "file",
                ("memory-file", path, len(body)),
                body if include_content else None,
                len(body),
                None,
            )
        prefix = "" if path == "." else path + "/"
        entries = {}
        for name in self.files:
            if name.startswith(prefix):
                child = name[len(prefix) :]
                entries[child.split("/", 1)[0]] = "directory" if "/" in child else "file"
        if path == ".":
            entries.update(self.ignored)
        if entries or path == ".":
            children = tuple(sorted(entries.items()))
            return _Observed(
                "directory", ("memory-dir", path, children), None, len(children), children
            )
        return _Observed("missing", (path,), None, 0, None)

    def close(self):
        self.closed = True


@pytest.fixture
def port(tmp_path):
    root = _MemoryWindowsRoot(tmp_path)
    port = WindowsReadPort(tmp_path, denied_paths=("private",), root_factory=lambda _: root)
    try:
        yield port
    finally:
        port.close()
        assert root.closed


def test_windows_legacy_json_bytes_and_new_snapshot_opt_in(port, monkeypatch):
    root = port.root_handle
    root.ignored.clear()
    monkeypatch.setattr(
        search, "hashlib", SimpleNamespace(sha256=lambda _: pytest.fail("旧模式不新增摘要"))
    )
    output = windows_search.grep_files(
        port, GrepInput(query="needle"), ReadOperation(), capture=None
    )
    assert type(output) is GrepOutput and type(output.matches[0]) is GrepMatch
    assert (
        output.model_dump_json().encode()
        == LEGACY_GREP_JSON.replace("REVISION", output.matches[0].revision).encode()
    )
    assert root.calls.count(("source.py", True, MAX_SEARCH_FILE_BYTES)) == 1


@pytest.mark.parametrize("options", [{}, {"max_results": 200, "context_lines": 0}])
def test_windows_snapshot_defaults_and_explicit_zero(port, options):
    port.root_handle.files = {"source.py": b"before\n" + b"needle\n" * 12 + b"after\n"}
    output = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle", **options), ReadOperation(), capture=None
    )
    assert type(output) is GrepSnapshotOutput
    assert output.spec_version == "harnessix.grep-snapshot/v1"
    assert len(output.matches) == (12 if options else 10)
    assert output.truncated == (not options)
    assert all(type(hit) is GrepSnapshotMatch for hit in output.matches)
    assert all((hit.context is None) == bool(options) for hit in output.matches)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"before\r\nneedle\r\nafter\r",
        b"\xef\xbb\xbfneedle\r\n",
        "needle é e\u0301 中文🙂\r\nneedle\u2028尾\n".encode(),
        b"needle\r",
    ],
)
def test_windows_uses_exact_same_observation_for_hash_identity_and_matches(port, monkeypatch, body):
    root = port.root_handle
    root.files = {"source.py": body}
    hashed = []

    def sha256(content):
        hashed.append(content)
        return hashlib.sha256(content)

    monkeypatch.setattr(search, "hashlib", SimpleNamespace(sha256=sha256))
    output = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=None
    )
    assert output.stats.files_read == 1 and output.stats.bytes_read == len(body)
    assert hashed == [body] and hashed[0] is body
    assert root.calls.count(("source.py", True, MAX_SEARCH_FILE_BYTES)) == 1
    observed = _Observed("file", ("memory-file", "source.py", len(body)), body, len(body), None)
    expected_revision = file_revision(port, "source.py", observed)
    for hit in output.matches:
        assert hit.content_sha256 == hashlib.sha256(body).hexdigest()
        assert hit.file_bytes == len(body)
        assert hit.revision == expected_revision and hit.revision != hit.content_sha256


@pytest.mark.parametrize("maximum,reason", [(1, "result_limit"), (200, "output_limit")])
def test_windows_capture_contains_all_new_match_fields_outside_preview(port, maximum, reason):
    body = ("needle" + "\t" * 378 + "\r\n").encode() * 40
    port.root_handle.files = {"source.py": body}
    capture = search.SearchCapture()
    output = windows_search.grep_files(
        port,
        GrepSnapshotInput(query="needle", max_results=maximum, context_lines=3),
        ReadOperation(),
        capture=capture,
    )
    hits = [GrepSnapshotMatch.model_validate_json(line) for line in capture.body.splitlines()]
    assert output.truncation_reason == reason and not output.scan_complete
    assert capture.complete and capture.records == 40
    assert [hit.line for hit in hits] == list(range(1, 41))
    assert tuple(hits[: len(output.matches)]) == output.matches
    assert all(hit.content_sha256 == hashlib.sha256(body).hexdigest() for hit in hits)
    assert all(hit.file_bytes == len(body) for hit in hits)
    assert sum(len(hit.model_dump_json().encode()) + 1 for hit in output.matches) <= (
        windows_search.MAX_SEARCH_RECORD_BYTES
    )
    assert port.root_handle.calls.count(("source.py", True, MAX_SEARCH_FILE_BYTES)) == 1


def test_windows_truncated_fragment_still_hashes_complete_file(port):
    body = ("前" * 800 + "needle" + "后" * 300 + "\r\n尾行\r\n").encode()
    port.root_handle.files = {"source.py": body}
    output = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=None
    )
    hit = output.matches[0]
    assert hit.text_truncated and output.scan_complete
    assert hit.content_sha256 == hashlib.sha256(body).hexdigest()
    assert hit.content_sha256 != hashlib.sha256(hit.text.encode()).hexdigest()
    assert hit.file_bytes == len(body)


@pytest.mark.parametrize("change", ["identity", "size", "missing", "link", "content_missing"])
def test_windows_unstable_or_incomplete_observation_is_not_hashed_or_captured(
    port, monkeypatch, change
):
    root = port.root_handle
    observe = root.observe
    capture = search.SearchCapture()

    def changed(path, **kwargs):
        observed = observe(path, **kwargs)
        if path == "source.py" and kwargs["include_content"]:
            if change == "identity":
                return replace(observed, identity=("replacement",))
            if change == "size":
                return replace(observed, size=observed.size + 1)
            if change == "missing":
                return replace(observed, kind="missing")
            if change == "link":
                raise KernelError("workspace_path_denied", "reparse point")
            return replace(observed, content=None)
        return observed

    monkeypatch.setattr(root, "observe", changed)
    monkeypatch.setattr(
        search, "hashlib", SimpleNamespace(sha256=lambda _: pytest.fail("不完整或漂移观察不得摘要"))
    )
    with pytest.raises(ReadToolError) as failed:
        windows_search.grep_files(
            port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture
        )
    assert failed.value.code == (
        "io_failed" if change == "content_missing" else "workspace_changed"
    )
    assert capture.records == 0 and not capture.complete


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("stage", ["content", "before_hash", "after_hash", "revision_hash"])
def test_windows_read_and_hash_checkpoints_do_not_publish_partial_success(
    port, monkeypatch, stop, stage
):
    operation, capture = ReadOperation(), search.SearchCapture()
    root, hashed = port.root_handle, []
    observe, decode, revision = (
        root.observe,
        windows_search._decode_search,
        windows_search.file_revision,
    )

    def halt():
        if stop == "cancel":
            operation.stopped.set()
        else:
            operation.deadline = 0

    def observing(path, **kwargs):
        observed = observe(path, **kwargs)
        if stage == "content" and kwargs["include_content"]:
            halt()
            kwargs["checkpoint"]()
        return observed

    def decoding(content, scan):
        text = decode(content, scan)
        if stage == "before_hash":
            halt()
        return text

    def revision_digest(*args):
        value = revision(*args)
        if stage == "revision_hash":
            halt()
        return value

    def sha256(content):
        hashed.append(content)
        if stage == "after_hash":
            halt()
        return hashlib.sha256(content)

    monkeypatch.setattr(root, "observe", observing)
    monkeypatch.setattr(windows_search, "_decode_search", decoding)
    monkeypatch.setattr(windows_search, "file_revision", revision_digest)
    monkeypatch.setattr(search, "hashlib", SimpleNamespace(sha256=sha256))
    with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as failed:
        windows_search.grep_files(
            port, GrepSnapshotInput(query="needle"), operation, capture=capture
        )
    if stop == "timeout":
        assert failed.value.code == "timeout"
    assert len(hashed) == (1 if stage == "after_hash" else 0)
    assert capture.records == 0 and not capture.complete


def test_windows_binary_invalid_utf8_oversize_and_line_gaps_never_claim_complete_capture(port):
    root = port.root_handle
    root.files = {
        "a.py": b"needle\n\xff",
        "b.py": b"needle\n\0",
        "c.py": b"x" * (MAX_SEARCH_FILE_BYTES + 1),
        "d.py": b"needle" + b"x" * MAX_LINE_BYTES + b"\nneedle\n",
        "unreadable.py": b"needle\n",
    }
    root.unreadable.add("unreadable.py")
    capture = search.SearchCapture()
    output = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture
    )
    assert not output.truncated and not output.scan_complete and not capture.complete
    assert output.stats.binary_files == output.stats.invalid_utf8_files == 1
    assert output.stats.oversized_files == output.stats.long_lines == 1
    assert output.stats.unreadable_entries == 1 and output.stats.ignored_entries == 2
    assert [(hit.path, hit.line) for hit in output.matches] == [("d.py", 2)]
    assert output.matches[0].content_sha256 == hashlib.sha256(root.files["d.py"]).hexdigest()
    assert GrepSnapshotMatch.model_validate_json(capture.body) == output.matches[0]
    assert ("c.py", True, MAX_SEARCH_FILE_BYTES) not in root.calls


def test_windows_exact_file_limit_and_original_total_budget(port, monkeypatch):
    body = b"needle\n" + b"x" * (MAX_SEARCH_FILE_BYTES - 7)
    port.root_handle.files = {"source.py": body}
    output = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=None
    )
    assert output.stats.oversized_files == 0 and output.stats.long_lines == 1
    assert output.matches[0].file_bytes == MAX_SEARCH_FILE_BYTES
    assert output.matches[0].content_sha256 == hashlib.sha256(body).hexdigest()
    port.root_handle.files = {"a.py": b"needle\n" * 2, "b.py": b"needle\n" * 2}
    monkeypatch.setattr(windows_search, "MAX_SEARCH_TOTAL_BYTES", 20)
    capture = search.SearchCapture()
    with pytest.raises(ReadToolError) as failed:
        windows_search.grep_files(
            port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture
        )
    assert failed.value.code == "limit_exceeded" and not capture.complete
    assert ("b.py", True, MAX_SEARCH_FILE_BYTES) not in port.root_handle.calls


@pytest.mark.parametrize("budget", ["MAX_ARTIFACT_RECORDS", "MAX_ARTIFACT_BYTES"])
def test_windows_capture_limit_does_not_return_prefix_as_complete_archive(
    port, monkeypatch, budget
):
    port.root_handle.files = {"source.py": b"needle\n" * 2}
    monkeypatch.setattr(search, budget, 1)
    capture = search.SearchCapture()
    with pytest.raises(ReadToolError) as failed:
        windows_search.grep_files(
            port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture
        )
    assert failed.value.code == "limit_exceeded" and not capture.complete


def test_windows_explicit_scope_include_ignored_and_no_reparse_following(port):
    root = port.root_handle
    root.files = {
        "src/a.py": b"needle\n",
        "src/b.txt": b"needle\n",
        "node_modules/dep.py": b"needle\n",
        ".env": b"needle SECRET\n",
        "private/a.py": b"needle SECRET\n",
    }
    scoped = windows_search.grep_files(
        port,
        GrepSnapshotInput(query="needle", path="src", include="*.py"),
        ReadOperation(),
        capture=None,
    )
    assert [hit.path for hit in scoped.matches] == ["src/a.py"]
    root.calls.clear()
    normal = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle"), ReadOperation(), capture=None
    )
    assert [hit.path for hit in normal.matches] == ["src/a.py", "src/b.txt"]
    assert normal.scan_complete and normal.stats.ignored_entries == 5
    assert not any(
        path in {"link.py", "hardlink.py", ".env", "private", "node_modules"}
        for path, _, _ in root.calls
    )
    included = windows_search.grep_files(
        port, GrepSnapshotInput(query="needle", include_ignored=True), ReadOperation(), capture=None
    )
    assert [hit.path for hit in included.matches] == [
        "node_modules/dep.py",
        "src/a.py",
        "src/b.txt",
    ]


@pytest.mark.parametrize("path", [".env", "private/a.py", "../outside", "a\\b"])
def test_windows_snapshot_path_policy_rejects_before_observation(port, path):
    with pytest.raises(ReadToolError) as failed:
        windows_search.grep_files(
            port, GrepSnapshotInput(query="needle", path=path), ReadOperation(), capture=None
        )
    assert failed.value.code == "path_denied" and not port.root_handle.calls
