"""POSIX真实文件：单次完整读取摘要、旧合同兼容及快照失败边界。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.tools import search
from harnessix.tools.contracts import MAX_LINE_BYTES, ReadToolError
from harnessix.tools.files import _decode
from harnessix.tools.search_contracts import (
    MAX_GREP_CONTEXT_LINES,
    MAX_MATCH_BYTES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_TOTAL_BYTES,
    ArchivedGrepOutput,
    ArchivedGrepSnapshotOutput,
    GrepInput,
    GrepMatch,
    GrepOutput,
    GrepSnapshotInput,
    GrepSnapshotMatch,
    GrepSnapshotOutput,
)
from harnessix.tools.workspace import ReadOperation, Workspace

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX搜索快照端口")

LEGACY_BODY = "before\r\nneedle é\r\nafter\r".encode()
LEGACY_GREP_JSON = (
    '{"path":".","stats":{"entries_scanned":1,"files_read":1,"bytes_read":25,'
    '"ignored_entries":0,"unreadable_entries":0,"oversized_files":0,"invalid_utf8_files":0,'
    '"binary_files":0,"long_lines":0},"scan_complete":true,"truncated":false,'
    '"truncation_reason":null,"matches":[{"path":"source.py","line":2,"text":"needle é",'
    '"text_truncated":false,"revision":"REVISION","context":null}]}'
)


def test_legacy_json_bytes_and_defaults_remain_unchanged(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_bytes(LEGACY_BODY)
    monkeypatch.setattr(
        search, "hashlib", SimpleNamespace(sha256=lambda _: pytest.fail("旧模式不新增内容摘要"))
    )
    with Workspace(tmp_path) as workspace:
        legacy = search.grep(workspace, GrepInput(query="needle"), ReadOperation())
        explicit = search.grep(
            workspace, GrepInput(query="needle", context_lines=0), ReadOperation()
        )
    assert type(legacy) is GrepOutput and type(legacy.matches[0]) is GrepMatch
    assert GrepInput(query="needle").max_results == 100
    assert GrepInput(query="needle").context_lines == 0
    assert legacy.model_dump_json() == explicit.model_dump_json()
    assert (
        legacy.model_dump_json().encode()
        == LEGACY_GREP_JSON.replace("REVISION", legacy.matches[0].revision).encode()
    )


@pytest.mark.parametrize("options", [{}, {"max_results": 200, "context_lines": 0}])
def test_snapshot_defaults_and_explicit_zero_are_respected(tmp_path, options):
    (tmp_path / "source.py").write_bytes(b"before\n" + b"needle\n" * 12 + b"after\n")
    args = GrepSnapshotInput(query="needle", **options)
    with Workspace(tmp_path) as workspace:
        output = search.grep(workspace, args, ReadOperation())
    assert type(output) is GrepSnapshotOutput
    assert output.spec_version == "harnessix.grep-snapshot/v1"
    assert args.max_results == options.get("max_results", 10)
    assert args.context_lines == options.get("context_lines", 2)
    assert len(output.matches) == (12 if options else 10)
    assert output.truncated == (not options)
    assert output.truncation_reason == (None if options else "result_limit")
    assert all(type(hit) is GrepSnapshotMatch for hit in output.matches)
    assert all((hit.context is None) == bool(options) for hit in output.matches)
    if not options:
        assert output.matches[0].context.start_line == 1
        assert len(output.matches[0].context.lines) == 4


@pytest.mark.parametrize("maximum", [1, 10, 200])
@pytest.mark.parametrize("radius", [0, 1, 2, MAX_GREP_CONTEXT_LINES])
def test_snapshot_explicit_input_bounds(maximum, radius):
    args = GrepSnapshotInput(
        query="needle",
        path="src",
        include="*.py",
        include_ignored=True,
        max_results=maximum,
        context_lines=radius,
    )
    assert (args.max_results, args.context_lines) == (maximum, radius)
    assert (args.path, args.include, args.include_ignored) == ("src", "*.py", True)


@pytest.mark.parametrize(
    "invalid",
    [
        {"max_results": 0},
        {"max_results": 201},
        {"max_results": True},
        {"context_lines": -1},
        {"context_lines": 4},
        {"context_lines": True},
        {"context_lines": "0"},
        {"context_lines": 0.0},
        {"context_lines": None},
        {"query": "x\ny"},
        {"include": "../*"},
        {"write": True},
    ],
)
def test_snapshot_reuses_strict_legacy_input_validation(invalid):
    with pytest.raises(ValidationError):
        GrepSnapshotInput.model_validate({"query": "needle", **invalid})


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"before\r\nneedle\r\nafter\r",
        b"\xef\xbb\xbfneedle\r\n",
        "needle é e\u0301 中文🙂\r\nneedle\u2028尾\n".encode(),
        b"needle\nneedle\n",
        b"needle\r",
    ],
)
def test_snapshot_hash_is_exact_complete_raw_utf8_not_revision(tmp_path, body):
    (tmp_path / "source.py").write_bytes(body)
    assert _decode(body).encode("utf-8") == body
    with Workspace(tmp_path) as workspace:
        output = search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation())
        legacy = search.grep(workspace, GrepInput(query="needle", context_lines=2), ReadOperation())
    assert output.stats.bytes_read == len(body) and output.stats.files_read == 1
    assert output.scan_complete
    assert [hit.revision for hit in output.matches] == [hit.revision for hit in legacy.matches]
    for hit in output.matches:
        assert hit.content_sha256 == hashlib.sha256(body).hexdigest()
        assert hit.file_bytes == len(body) and hit.content_sha256 != hit.revision
    if body == b"\xef\xbb\xbfneedle\r\n":
        assert output.matches[0].text.startswith("\ufeff")
        assert output.matches[0].content_sha256 != hashlib.sha256(b"needle\n").hexdigest()


def test_snapshot_reads_and_hashes_content_once_even_when_preview_stops(tmp_path, monkeypatch):
    body = b"unused\n" * 12000 + b"needle\n" * 20
    (tmp_path / "source.py").write_bytes(body)
    real_read, real_open, real_decode = os.read, os.open, search._decode
    reads, opens, decoded, hashed = [], [], [], []

    def read(fd, size):
        data = real_read(fd, size)
        reads.append((fd, data))
        return data

    def open_file(path, flags, *args, **kwargs):
        if path == "source.py":
            assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) == 0
            opens.append(path)
        return real_open(path, flags, *args, **kwargs)

    def decode(data):
        decoded.append(data)
        return real_decode(data)

    def sha256(data):
        hashed.append(data)
        return hashlib.sha256(data)

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "lseek", lambda *_: pytest.fail("不得回卷并重新读取"))
    monkeypatch.setattr(search, "_decode", decode)
    monkeypatch.setattr(search, "hashlib", SimpleNamespace(sha256=sha256))
    with Workspace(tmp_path) as workspace:
        output = search.grep(
            workspace, GrepSnapshotInput(query="needle", max_results=1), ReadOperation()
        )
    assert output.truncation_reason == "result_limit"
    assert len(opens) == 1  # 目录发现仅stat；内容文件仅打开一次。
    assert len({fd for fd, _ in reads}) == 1
    assert b"".join(data for _, data in reads) == body
    assert reads[-1][1] == b"" and len(reads) == 3
    assert decoded == hashed == [body]
    assert output.stats.files_read == 1 and output.stats.bytes_read == len(body)
    assert output.matches[0].file_bytes == len(body)


@pytest.mark.parametrize("maximum,reason", [(1, "result_limit"), (200, "output_limit")])
def test_capture_archives_all_snapshot_matches_despite_preview_truncation(
    tmp_path, maximum, reason
):
    body = ("needle" + "\t" * 378 + "\r\n").encode() * 40
    (tmp_path / "source.py").write_bytes(body)
    capture = search.SearchCapture()
    with Workspace(tmp_path) as workspace:
        output = search.grep(
            workspace,
            GrepSnapshotInput(query="needle", context_lines=3, max_results=maximum),
            ReadOperation(),
            capture=capture,
        )
    assert output.truncation_reason == reason and not output.scan_complete
    assert sum(len(hit.model_dump_json().encode()) + 1 for hit in output.matches) <= (
        search.MAX_SEARCH_RECORD_BYTES
    )
    hits = [GrepSnapshotMatch.model_validate_json(line) for line in capture.body.splitlines()]
    assert capture.complete and capture.records == 40
    assert [hit.line for hit in hits] == list(range(1, 41))
    assert tuple(hits[: len(output.matches)]) == output.matches
    assert all(hit.content_sha256 == hashlib.sha256(body).hexdigest() for hit in hits)
    assert all(hit.file_bytes == len(body) and hit.context is not None for hit in hits)
    artifact = ArtifactRef(
        artifact_id=UUID(int=1),
        sha256=hashlib.sha256(capture.body).hexdigest(),
        size_bytes=len(capture.body),
        records=capture.records,
        complete=capture.complete,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    archived = ArchivedGrepSnapshotOutput(preview=output, artifact=artifact)
    restored = ArchivedGrepSnapshotOutput.model_validate_json(archived.model_dump_json())
    assert restored == archived
    assert type(restored.preview.matches[0]) is GrepSnapshotMatch


def test_truncated_text_does_not_turn_full_digest_into_fragment_digest(tmp_path):
    body = ("前" * 800 + "needle" + "后" * 300 + "\r\n尾行\r\n").encode()
    (tmp_path / "source.py").write_bytes(body)
    with Workspace(tmp_path) as workspace:
        output = search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation())
    hit = output.matches[0]
    assert output.scan_complete and hit.text_truncated
    assert len(hit.text.encode()) <= MAX_MATCH_BYTES and "needle" in hit.text
    assert hit.content_sha256 == hashlib.sha256(body).hexdigest()
    assert hit.content_sha256 != hashlib.sha256(hit.text.encode()).hexdigest()
    assert hit.file_bytes == len(body)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("stage", ["read", "before_hash", "after_hash"])
def test_snapshot_checkpoints_abort_without_leaking_fd(tmp_path, monkeypatch, stop, stage):
    (tmp_path / "source.py").write_bytes(b"needle\n" * 12000)
    operation, capture = ReadOperation(), search.SearchCapture()
    real_read, real_load = os.read, search._read
    observed_fds, hashed = [], []

    def halt():
        if stop == "cancel":
            operation.stopped.set()
        else:
            operation.deadline = 0

    def read(fd, size):
        data = real_read(fd, size)
        if stage == "read":
            halt()
        return data

    def load(fd, op, scan):
        observed_fds.append(fd)
        text = real_load(fd, op, scan)
        if stage == "before_hash":
            halt()
        return text

    def sha256(data):
        hashed.append(data)
        if stage == "after_hash":
            halt()
        return hashlib.sha256(data)

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(search, "_read", load)
    monkeypatch.setattr(search, "hashlib", SimpleNamespace(sha256=sha256))
    with Workspace(tmp_path) as workspace:
        with pytest.raises(TurnCancelled if stop == "cancel" else ReadToolError) as failed:
            search.grep(workspace, GrepSnapshotInput(query="needle"), operation, capture=capture)
        if stop == "timeout":
            assert failed.value.code == "timeout"
        with pytest.raises(OSError):
            os.fstat(observed_fds[0])
    assert len(hashed) == (1 if stage == "after_hash" else 0)
    assert capture.records == 0 and not capture.complete


@pytest.mark.parametrize("mutation", ["rewrite", "grow", "shrink", "replace"])
def test_same_fd_revision_drift_is_rejected_before_hash_or_capture(tmp_path, monkeypatch, mutation):
    target = tmp_path / "source.py"
    target.write_bytes(b"needle\n")
    before = target.stat()
    real_load = search._read
    capture = search.SearchCapture()

    def changing_load(fd, operation, scan):
        text = real_load(fd, operation, scan)
        if mutation == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"absent\n")
            os.replace(replacement, target)
        else:
            target.write_bytes(
                {"rewrite": b"absent\n", "grow": b"needle\nextra\n", "shrink": b"n"}[mutation]
            )
            if mutation == "rewrite":
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return text

    monkeypatch.setattr(search, "_read", changing_load)
    monkeypatch.setattr(
        search, "hashlib", SimpleNamespace(sha256=lambda _: pytest.fail("漂移必须在摘要前拒绝"))
    )
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture)
    assert failed.value.code == "workspace_changed"
    assert capture.records == 0 and not capture.complete


@pytest.mark.parametrize("snapshot", [False, True])
def test_extra_fd_state_check_is_snapshot_only(tmp_path, monkeypatch, snapshot):
    (tmp_path / "source.py").write_bytes(b"needle\n")
    real_state = search.revision_state
    calls = []

    def drift(info):
        state = real_state(info)
        calls.append(state)
        return state if len(calls) == 1 else (*state[:-1], state[-1] + 1)

    monkeypatch.setattr(search, "revision_state", drift)
    with Workspace(tmp_path) as workspace:
        if snapshot:
            with pytest.raises(ReadToolError) as failed:
                search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation())
            assert failed.value.code == "workspace_changed" and len(calls) == 2
        else:
            output = search.grep(workspace, GrepInput(query="needle"), ReadOperation())
            assert output.scan_complete and len(calls) == 1


def test_gaps_do_not_claim_complete_scan_or_archive_but_hit_hash_is_complete(tmp_path, monkeypatch):
    bodies = {
        "a.py": b"needle\n\xff",
        "b.py": b"needle\n\0",
        "c.py": b"x" * (MAX_SEARCH_FILE_BYTES + 1),
        "d.py": b"needle" + b"x" * MAX_LINE_BYTES + b"\nneedle\n",
        "unreadable.py": b"needle\n",
    }
    for name, body in bodies.items():
        (tmp_path / name).write_bytes(body)
    real_open = os.open

    def denied(path, *args, **kwargs):
        if path == "unreadable.py":
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    capture = search.SearchCapture()
    with Workspace(tmp_path) as workspace:
        output = search.grep(
            workspace, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture
        )
    assert not output.truncated and not output.scan_complete and not capture.complete
    assert [(hit.path, hit.line) for hit in output.matches] == [("d.py", 2)]
    assert output.stats.invalid_utf8_files == output.stats.binary_files == 1
    assert output.stats.oversized_files == output.stats.long_lines == 1
    assert output.stats.unreadable_entries == 1
    assert output.matches[0].content_sha256 == hashlib.sha256(bodies["d.py"]).hexdigest()
    assert GrepSnapshotMatch.model_validate_json(capture.body) == output.matches[0]


def test_original_file_limit_includes_exact_boundary(tmp_path):
    assert MAX_SEARCH_FILE_BYTES == 2 * 1024 * 1024
    assert MAX_SEARCH_TOTAL_BYTES == 16 * 1024 * 1024
    body = b"needle\n" + b"x" * (MAX_SEARCH_FILE_BYTES - 7)
    (tmp_path / "source.py").write_bytes(body)
    with Workspace(tmp_path) as workspace:
        output = search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation())
    assert output.stats.oversized_files == 0 and output.stats.long_lines == 1
    assert output.matches[0].file_bytes == MAX_SEARCH_FILE_BYTES
    assert output.matches[0].content_sha256 == hashlib.sha256(body).hexdigest()


def test_total_scan_and_capture_budgets_never_publish_partial_success(tmp_path, monkeypatch):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_bytes(b"needle\n" * 2)
    monkeypatch.setattr(search, "MAX_SEARCH_TOTAL_BYTES", 20)
    capture = search.SearchCapture()
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture)
    assert failed.value.code == "limit_exceeded" and not capture.complete
    monkeypatch.setattr(search, "MAX_SEARCH_TOTAL_BYTES", MAX_SEARCH_TOTAL_BYTES)
    monkeypatch.setattr(search, "MAX_ARTIFACT_RECORDS", 1)
    capture = search.SearchCapture()
    with Workspace(tmp_path) as workspace, pytest.raises(ReadToolError) as failed:
        search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation(), capture=capture)
    assert failed.value.code == "limit_exceeded" and not capture.complete


def test_snapshot_keeps_explicit_scope_pattern_ignores_and_no_link_following(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/source.py").write_bytes(b"needle\n")
    (root / "src/other.txt").write_bytes(b"needle\n")
    (root / "node_modules").mkdir()
    (root / "node_modules/dep.py").write_bytes(b"needle\n")
    (root / ".env").write_bytes(b"needle SECRET\n")
    (tmp_path / "outside.py").write_bytes(b"needle SECRET\n")
    (root / "link.py").symlink_to(tmp_path / "outside.py")
    os.link(tmp_path / "outside.py", root / "hardlink.py")
    with Workspace(root) as workspace:
        scoped = search.grep(
            workspace,
            GrepSnapshotInput(query="needle", path="src", include="*.py"),
            ReadOperation(),
        )
        normal = search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation())
        included = search.grep(
            workspace, GrepSnapshotInput(query="needle", include_ignored=True), ReadOperation()
        )
    assert [hit.path for hit in scoped.matches] == ["src/source.py"]
    assert [hit.path for hit in normal.matches] == ["src/other.txt", "src/source.py"]
    assert [hit.path for hit in included.matches] == [
        "node_modules/dep.py",
        "src/other.txt",
        "src/source.py",
    ]
    assert normal.scan_complete and normal.stats.ignored_entries == 4


@pytest.mark.parametrize(
    "invalid",
    [
        {"content_sha256": None},
        {"content_sha256": "invalid"},
        {"file_bytes": -1},
        {"file_bytes": MAX_SEARCH_FILE_BYTES + 1},
        {"file_bytes": True},
        {"file_bytes": "7"},
        {"file_bytes": 7.0},
        {"unexpected": True},
    ],
)
def test_snapshot_match_rejects_forged_or_unbounded_fields(invalid):
    valid = dict(
        path="source.py",
        line=1,
        text="needle",
        text_truncated=False,
        revision="a" * 64,
        content_sha256="b" * 64,
        file_bytes=7,
    )
    with pytest.raises(ValidationError):
        GrepSnapshotMatch.model_validate({**valid, **invalid})
    assert GrepSnapshotMatch.model_validate({**valid, "file_bytes": 0}).file_bytes == 0


def test_snapshot_output_requires_version_new_matches_and_inherited_consistency(tmp_path):
    (tmp_path / "source.py").write_bytes(b"needle\nneedle\n")
    with Workspace(tmp_path) as workspace:
        output = search.grep(workspace, GrepSnapshotInput(query="needle"), ReadOperation())
        legacy = search.grep(workspace, GrepInput(query="needle"), ReadOperation())
    data = output.model_dump()
    assert GrepSnapshotOutput.model_validate_json(output.model_dump_json()) == output
    for changes in (
        {"spec_version": "harnessix.grep-snapshot/v2"},
        {"matches": legacy.matches},
        {"matches": tuple(reversed(output.matches))},
        {"matches": output.matches * 2},
        {"matches": output.matches * 101},
        {"scan_complete": False},
        {"truncated": True},
    ):
        with pytest.raises(ValidationError):
            GrepSnapshotOutput.model_validate({**data, **changes})
    corrupt = output.matches[0].model_dump()
    corrupt["context"]["lines"][0]["text"] = "forged"
    with pytest.raises(ValidationError):
        GrepSnapshotMatch.model_validate(corrupt)


@pytest.mark.parametrize(
    "name,model",
    [
        ("grep-input", GrepInput),
        ("grep-output", GrepOutput),
        ("archived-grep-output", ArchivedGrepOutput),
    ],
)
def test_legacy_schemas_are_byte_frozen(name, model):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v2.schema.json"
    assert json.loads(path.read_bytes()) == model.model_json_schema()
