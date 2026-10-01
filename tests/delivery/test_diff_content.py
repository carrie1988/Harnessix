"""唯一Diff编码器与固定原Workspace golden；不复制生产算法或构造授权证明。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceFileVersion, WorkspaceMutation
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.diff_content import (
    DiffContentLimitError,
    WorkspaceDiffContent,
    build_diff_content,
)
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore

GOLDEN = Path(__file__).with_name("fixtures") / "workspace-diff-content-d3175f7-v1.json"
MAX_DIFF = 64 * 1024 * 1024
MARKER = "\\ No newline at end of file\n"


class IntSubclass(int):
    pass


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _version(body=None, mode=420):
    if body is None:
        return WorkspaceFileVersion(presence="absent", size=0)
    return WorkspaceFileVersion(presence="file", sha256=_sha(body), size=len(body), mode=mode)


def _mutation(path="f", before=None, after=b"new\n", before_mode=420, after_mode=420):
    return WorkspaceMutation(
        path=path, before=_version(before, before_mode), after=_version(after, after_mode)
    )


def _reader(*bodies):
    blobs = {_sha(body): body for body in bodies if body is not None}
    return blobs.__getitem__


def _checkpoint():
    return None


def _render(mutations, read_blob, *, limit=None, markers=False, checkpoint=_checkpoint):
    return build_diff_content(
        mutations,
        read_blob,
        max_utf8_bytes=limit,
        checkpoint=checkpoint,
        missing_newline_markers=markers,
    )


def test_fixed_original_workspace_golden_is_not_regenerated_by_new_renderer():
    fixture_body = GOLDEN.read_bytes()
    assert _sha(fixture_body) == "795715bf550233dd18ee241fe42d7f36963eb2c68546f4ab0d4efb83e8b17557"
    golden = json.loads(fixture_body)
    assert golden["source_commit"] == "d3175f71d77cfb0f5f2c2a613e6da3c74ae68204"
    assert (
        golden["source_sha256"]
        == "9adb8d13aa1a8cf34d190e6e287d6957e3c5dc010296e3f2f72621028c29fe0e"
    )
    assert golden["source_path"] == "src/harnessix/delivery/diff.py"
    assert not any(
        value in fixture_body for value in (b"/Users/", b"transaction_id", b"fingerprint")
    )
    mutations = tuple(WorkspaceMutation.model_validate(row) for row in golden["mutations"])
    blobs = {digest: bytes.fromhex(body) for digest, body in golden["blobs"].items()}
    assert all(_sha(body) == digest for digest, body in blobs.items())
    result = _render(mutations, blobs.__getitem__)
    expected = golden["expected"]
    assert [row.model_dump(mode="json") for row in result.entries] == expected["entries"]
    assert result.text.encode() == expected["text"].encode()
    assert result.utf8_bytes == expected["utf8_bytes"] == 1835
    assert result.sha256 == expected["sha256"]
    assert MARKER not in result.text
    assert "-旧行🙂+新行中文" in result.text
    assert " same\r\n-old\r\n+new\r\n" in result.text
    assert " same\r\n-old+new" in result.text


def test_real_workspace_facade_preserves_transaction_identity_and_legacy_missing_lf(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "f").write_bytes(b"before")
    (workspace / "f").chmod(0o644)
    prepared = prepare_workspace_transaction(
        workspace,
        {"f": DesiredWorkspaceFile(b"after", 420)},
        request_id="content-facade",
    )
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        record = store.save(prepared)
        result = build_workspace_diff(record.plan, store)
        content = _render(record.plan.mutations, store.blob)
    assert result.transaction_id == record.plan.transaction_id
    assert result.plan_fingerprint == record.plan.fingerprint
    assert (result.entries, result.text, result.utf8_bytes, result.sha256) == (
        content.entries,
        content.text,
        content.utf8_bytes,
        content.sha256,
    )
    assert "-before+after" in result.text and MARKER not in result.text


@pytest.mark.parametrize(
    "before,after,before_mode,after_mode,kind,binary",
    [
        (None, b"text\n", 420, 420, "added", False),
        (b"text\n", None, 420, 420, "deleted", False),
        (b"old\n", b"new\n", 420, 420, "modified", False),
        (None, b"", 420, 420, "added", False),
        (b"", None, 420, 420, "deleted", False),
        (b"same\n", b"same\n", 420, 493, "modified", False),
        (b"\xff", b"valid\n", 420, 420, "modified", True),
        (None, b"\0\xff", 420, 420, "added", True),
    ],
    ids=[
        "text-add",
        "text-delete",
        "text-edit",
        "empty-add",
        "empty-delete",
        "mode",
        "invalid-utf8",
        "nul",
    ],
)
def test_text_empty_binary_and_mode_entries_are_complete(
    before, after, before_mode, after_mode, kind, binary
):
    mutation = _mutation(before=before, after=after, before_mode=before_mode, after_mode=after_mode)
    result = _render((mutation,), _reader(before, after), markers=True)
    (entry,) = result.entries
    assert (entry.kind, entry.path, entry.before, entry.after, entry.binary) == (
        kind,
        "f",
        mutation.before,
        mutation.after,
        binary,
    )
    assert result.utf8_bytes == len(result.text.encode())
    assert result.sha256 == _sha(result.text.encode())
    if binary:
        assert "Binary files differ" in result.text and MARKER not in result.text
    if before == after:
        assert result.text == "diff --harnessix a/f b/f\nold mode 644\nnew mode 755\n"


@pytest.mark.parametrize("body", [b"exact\n", b"\0\xff"])
def test_unique_rename_reads_one_complete_body_and_preserves_original_paths(body):
    mutations = (_mutation("new", after=body), _mutation("old", before=body, after=None))
    calls = []

    def read(digest):
        calls.append(digest)
        return body

    result = _render(mutations, read, markers=True)
    assert calls == [_sha(body)]
    (entry,) = result.entries
    assert (entry.kind, entry.original_path, entry.path, entry.binary) == (
        "renamed",
        "old",
        "new",
        b"\0" in body,
    )
    assert result.text == (
        "diff --harnessix a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\n"
    )


def test_ambiguous_additions_stay_independent_and_legacy_deletion_order_is_preserved():
    body = b"same\n"
    ambiguous = (
        _mutation("a", after=body),
        _mutation("b", after=body),
        _mutation("old", before=body, after=None),
    )
    assert [entry.kind for entry in _render(ambiguous, _reader(body)).entries] == [
        "added",
        "added",
        "deleted",
    ]
    # 原算法按删除顺序消耗唯一新增项，不另行引入全局重命名求解器。
    legacy = (
        _mutation("a-old", before=body, after=None),
        _mutation("b-old", before=body, after=None),
        _mutation("new", after=body),
    )
    result = _render(legacy, _reader(body))
    assert [(e.kind, e.original_path, e.path) for e in result.entries] == [
        ("renamed", "a-old", "new"),
        ("deleted", None, "b-old"),
    ]


def test_equal_body_different_mode_is_not_rename():
    body = b"same\n"
    result = _render(
        (_mutation("new", after=body, after_mode=493), _mutation("old", before=body, after=None)),
        _reader(body),
    )
    assert [entry.kind for entry in result.entries] == ["added", "deleted"]


@pytest.mark.parametrize(
    "before,after,expected",
    [
        (None, b"new", "@@ -0,0 +1 @@\n+new\n" + MARKER),
        (b"old", None, "@@ -1 +0,0 @@\n-old\n" + MARKER),
        (b"old", b"new", "@@ -1 +1 @@\n-old\n" + MARKER + "+new\n" + MARKER),
        (b"old\n", b"new", "@@ -1 +1 @@\n-old\n+new\n" + MARKER),
        (b"old", b"new\n", "@@ -1 +1 @@\n-old\n" + MARKER + "+new\n"),
        (b"old\n", b"new\n", "@@ -1 +1 @@\n-old\n+new\n"),
        (b"old\r\n", b"new\r\n", "@@ -1 +1 @@\n-old\r\n+new\r\n"),
        (b"old\r\n", b"new", "@@ -1 +1 @@\n-old\r\n+new\n" + MARKER),
    ],
)
def test_newline_markers_are_independent_lines_and_legacy_is_unchanged(before, after, expected):
    mutation = _mutation(before=before, after=after)
    result = _render((mutation,), _reader(before, after), markers=True)
    assert result.text.endswith(expected)
    legacy = _render((mutation,), _reader(before, after))
    assert MARKER not in legacy.text
    assert result.entries == legacy.entries


@pytest.mark.parametrize("separator", ["\r", "\u2028", "\u2029"], ids=["CR", "U2028", "U2029"])
@pytest.mark.parametrize("terminal_lf", [True, False], ids=["terminal-LF", "missing-LF"])
def test_only_terminal_lf_controls_markers_not_middle_unicode_separators(separator, terminal_lf):
    ending = "\n" if terminal_lf else ""
    before = f"prefix{separator}old{ending}".encode()
    after = f"prefix{separator}new{ending}".encode()
    assert before.endswith(b"\n") is terminal_lf
    assert after.endswith(b"\n") is terminal_lf
    mutation = _mutation(before=before, after=after)
    header = "diff --harnessix a/f b/f\n--- a/f\n+++ b/f\n"
    # 旧表示按splitlines拆片，原字节行为不能随新LF语义改变。
    legacy = _render((mutation,), _reader(before, after), markers=False)
    assert legacy.text == (
        header + "@@ -1,2 +1,2 @@\n" + f" prefix{separator}-old{ending}+new{ending}"
    )
    assert MARKER not in legacy.text
    result = _render((mutation,), _reader(before, after), markers=True)
    assert result.entries == legacy.entries
    assert result.text.count(MARKER) == (0 if terminal_lf else 2)
    marker = "" if terminal_lf else MARKER
    # Git行只由LF结束；中间CR或Unicode separator必须保留在同一原始行内。
    assert result.text == (
        header + "@@ -1 +1 @@\n" + f"-prefix{separator}old\n{marker}+prefix{separator}new\n{marker}"
    )


@pytest.mark.parametrize("scenario", ["unicode", "mode", "rename", "missing-lf"])
@pytest.mark.parametrize("markers", [False, True])
def test_real_utf8_capacity_one_below_exact_and_one_above(scenario, markers):
    before, after = "原始🙂\n".encode(), "新文中文🙂\n".encode()
    if scenario == "rename":
        mutations = (_mutation("新🙂", after=before), _mutation("旧🙂", before=before, after=None))
    elif scenario == "mode":
        mutations = (_mutation("模式🙂", before, before, after_mode=493),)
    else:
        if scenario == "missing-lf":
            after = after.rstrip(b"\n")
        mutations = (_mutation("文件🙂", before, after),)
    reader = _reader(before, after)
    reference = _render(mutations, reader, markers=markers)
    assert reference.utf8_bytes > len(reference.text)
    assert _render(mutations, reader, limit=reference.utf8_bytes, markers=markers) == reference
    assert _render(mutations, reader, limit=reference.utf8_bytes + 1, markers=markers) == reference
    with pytest.raises(DiffContentLimitError) as error:
        _render(mutations, reader, limit=reference.utf8_bytes - 1, markers=markers)
    assert str(error.value) == "完整Diff超过声明容量"
    assert "文件" not in str(error.value) and "原始" not in str(error.value)


@pytest.mark.parametrize("limit", [True, False, 0, -1, MAX_DIFF + 1, 1.0, "1", IntSubclass(1)])
def test_invalid_actual_capacity_is_rejected_before_reader(limit):
    calls = []
    with pytest.raises(ValueError) as error:
        _render((_mutation(),), lambda digest: calls.append(digest) or b"new\n", limit=limit)
    assert type(error.value) is ValueError
    assert calls == []


@pytest.mark.parametrize("marker", [0, 1, None, "true"])
def test_newline_policy_requires_actual_bool_before_reader(marker):
    calls = []
    with pytest.raises(ValueError):
        _render((_mutation(),), lambda digest: calls.append(digest) or b"new\n", markers=marker)
    assert calls == []


def test_unlimited_legacy_and_maximum_explicit_budget_are_valid():
    mutations = (_mutation(),)
    expected = _render(mutations, _reader(b"new\n"))
    assert _render(mutations, _reader(b"new\n"), limit=MAX_DIFF) == expected
    assert type(expected) is WorkspaceDiffContent and not hasattr(expected, "__dict__")
    assert "text=" not in repr(expected) and "entries=" not in repr(expected)
    assert not hasattr(expected, "transaction_id") and not hasattr(expected, "plan_fingerprint")
    with pytest.raises(FrozenInstanceError):
        expected.text = "changed"


@pytest.mark.parametrize(
    "exception", [RuntimeError, TimeoutError, TurnCancelled, asyncio.CancelledError, KernelError]
)
@pytest.mark.parametrize("phase", ["first", "middle", "last"])
def test_checkpoints_propagate_same_exception_without_partial_content(exception, phase):
    bodies = (b"rename\n", b"old\n", b"new\n")
    mutations = (
        _mutation("a", after=bodies[0]),
        _mutation("b", bodies[1], bodies[2]),
        _mutation("z", bodies[0], None),
    )
    calls = []
    _render(mutations, _reader(*bodies), checkpoint=lambda: calls.append(None))
    target = {"first": 1, "middle": len(calls) // 2, "last": len(calls)}[phase]
    failure = (
        KernelError("diff_test_cancel", "合成取消") if exception is KernelError else exception()
    )
    count = 0

    def cancel():
        nonlocal count
        count += 1
        if count == target:
            raise failure

    with pytest.raises(exception) as error:
        _render(mutations, _reader(*bodies), checkpoint=cancel)
    assert error.value is failure


@pytest.mark.parametrize(
    "exception", [RuntimeError, TimeoutError, TurnCancelled, asyncio.CancelledError, KernelError]
)
@pytest.mark.parametrize("rename", [False, True])
def test_reader_failure_is_same_instance_and_not_capacity_or_partial_result(exception, rename):
    body = b"same\n"
    mutations = (
        (_mutation("a", after=body), _mutation("b", body, None)) if rename else (_mutation(),)
    )
    failure = (
        KernelError("diff_reader_failed", "合成读失败") if exception is KernelError else exception()
    )

    def read(_):
        raise failure

    with pytest.raises(exception) as error:
        _render(mutations, read)
    assert error.value is failure


@pytest.mark.parametrize("exception", [TurnCancelled, TimeoutError])
def test_rename_checkpoint_after_full_read_precedes_chunk_capacity(exception):
    body = b"same\n"
    failure = exception()
    cancelled = False

    def read(_):
        nonlocal cancelled
        cancelled = True
        return body

    def checkpoint():
        if cancelled:
            raise failure

    mutations = (_mutation("a", after=body), _mutation("b", body, None))
    with pytest.raises(exception) as error:
        _render(mutations, read, limit=1, checkpoint=checkpoint)
    assert error.value is failure
