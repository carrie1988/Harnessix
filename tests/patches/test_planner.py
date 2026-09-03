from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import planner
from harnessix.patches.contracts import ExactEdit, PatchManifest, PatchProposal
from harnessix.patches.planner import prepare_patch, verify_prepared
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace, digest, revision_state


def proposal(workspace, *, path="main.py", old="before", new="after", edits=None):
    page = read_file(workspace, ReadFileInput(path=path), ReadOperation())
    return PatchProposal(
        path=path,
        expected_revision=page.revision,
        edits=edits or (ExactEdit(old_text=old, new_text=new),),
    )


@pytest.mark.parametrize(
    "source",
    [
        b"before",
        b"before\n",
        b"before\r\n",
        b"\xef\xbb\xbfbefore\r\n",
        "中文 before\n末尾".encode(),
        b"before\r\nkeep\nlast\r\n",
    ],
)
def test_prepare_and_verify_never_write_and_preserve_unedited_bytes(tmp_path, source):
    path = tmp_path / "main.py"
    path.write_bytes(source)
    with Workspace(tmp_path) as workspace:
        prepared = prepare_patch(workspace, proposal(workspace), ReadOperation())
        verify_prepared(workspace, prepared, ReadOperation())
        assert prepared.before == source and prepared.after == source.replace(b"before", b"after")
        assert prepared.manifest.before_sha256 == hashlib.sha256(source).hexdigest()
        assert prepared.manifest.after_sha256 == hashlib.sha256(prepared.after).hexdigest()
        assert prepared.manifest.before_bytes == len(source)
        assert prepared.manifest.after_bytes == len(prepared.after)
        assert (
            PatchManifest.model_validate_json(prepared.manifest.model_dump_json())
            == prepared.manifest
        )
        assert "before'" not in repr(prepared) and "after'" not in repr(prepared)
    assert path.read_bytes() == source and sorted(p.name for p in tmp_path.iterdir()) == ["main.py"]
    with Workspace(tmp_path) as reopened:
        verify_prepared(reopened, prepared, ReadOperation())


def test_multiple_edits_use_original_coordinates_not_chained_replacement(tmp_path):
    (tmp_path / "main.py").write_text("abc def ghi")
    edits = (ExactEdit(old_text="abc", new_text="def"), ExactEdit(old_text="def", new_text="XYZ"))
    with Workspace(tmp_path) as workspace:
        first = prepare_patch(workspace, proposal(workspace, edits=edits), ReadOperation())
        second = prepare_patch(workspace, proposal(workspace, edits=edits[::-1]), ReadOperation())
    assert first.after == second.after == b"def XYZ ghi"
    assert first.manifest.fingerprint != second.manifest.fingerprint  # 绑定具体提案顺序。


@pytest.mark.parametrize(
    "source,edits,code",
    [
        ("abcdef", (("missing", "x"),), "patch_context_not_found"),
        ("before before", (("before", "x"),), "patch_ambiguous_context"),
        ("aaa", (("aa", "x"),), "patch_ambiguous_context"),
        ("abcdef", (("abc", "x"), ("cde", "y")), "patch_overlapping_edits"),
        ("ab", (("a", ""), ("b", "ab")), "patch_no_change"),
        ("é", (("e\u0301", "x"),), "patch_context_not_found"),
        ("before\r\n", (("before\n", "after\n"),), "patch_context_not_found"),
    ],
)
def test_invalid_matches_fail_without_modification(tmp_path, source, edits, code):
    path = tmp_path / "main.py"
    path.write_bytes(source.encode())
    with Workspace(tmp_path) as workspace:
        args = proposal(workspace, edits=tuple(ExactEdit(old_text=a, new_text=b) for a, b in edits))
        with pytest.raises(KernelError) as error:
            prepare_patch(workspace, args, ReadOperation())
    assert error.value.code == code and path.read_bytes() == source.encode()


@pytest.mark.parametrize("change", ["content", "mode", "inode", "root", "policy", "deleted"])
def test_verify_rejects_source_or_workspace_drift(tmp_path, change):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "main.py"
    path.write_text("before")
    with Workspace(root) as workspace:
        prepared = prepare_patch(workspace, proposal(workspace), ReadOperation())
    if change == "content":
        path.write_text("USER EDIT")
    elif change == "mode":
        path.chmod(path.stat().st_mode ^ 0o100)
    elif change == "inode":
        other = root / "replacement"
        other.write_text("before")
        other.replace(path)
    elif change == "root":
        root.rename(tmp_path / "old")
        root.mkdir()
        path.write_text("before")
    elif change == "deleted":
        path.unlink()
    with Workspace(root, denied_paths=("private",) if change == "policy" else ()) as workspace:
        with pytest.raises(KernelError) as error:
            verify_prepared(workspace, prepared, ReadOperation())
    assert error.value.code == (
        "patch_workspace_changed"
        if change in {"root", "policy"}
        else "patch_not_found"
        if change == "deleted"
        else "patch_source_changed"
    )
    if change == "content":
        assert path.read_text() == "USER EDIT"


@pytest.mark.parametrize("kind", ["before", "after", "manifest", "proposal", "mutable"])
def test_corrupt_plan_rejected_before_source_read(tmp_path, monkeypatch, kind):
    (tmp_path / "main.py").write_text("before")
    with Workspace(tmp_path) as workspace:
        prepared = prepare_patch(workspace, proposal(workspace), ReadOperation())
        if kind == "manifest":
            prepared = replace(
                prepared, manifest=prepared.manifest.model_copy(update={"edit_count": 2})
            )
        elif kind == "proposal":
            prepared = replace(
                prepared, proposal=prepared.proposal.model_copy(update={"path": "other.py"})
            )
        elif kind == "mutable":
            prepared = replace(prepared, before=bytearray(prepared.before))
        else:
            prepared = replace(prepared, **{kind: b"TAMPERED"})

        def forbidden(*args):
            pytest.fail("损坏计划不应重新读取源文件")

        monkeypatch.setattr(planner, "_read_image", forbidden)
        with pytest.raises(KernelError) as error:
            verify_prepared(workspace, prepared, ReadOperation())
        assert error.value.code == "patch_plan_corrupt"


@pytest.mark.parametrize("source", [b"before\n\xff", b"before\n\x00", b"before\n\x7f"])
def test_full_image_validation_includes_unpreviewed_tail(tmp_path, source):
    path = tmp_path / "main.py"
    path.write_bytes(source)
    with Workspace(tmp_path) as workspace:
        revision = digest((workspace.scope, "main.py", revision_state(path.stat())))
        args = PatchProposal(
            path="main.py",
            expected_revision=revision,
            edits=(ExactEdit(old_text="before", new_text="after"),),
        )
        with pytest.raises(KernelError) as error:
            prepare_patch(workspace, args, ReadOperation())
        assert error.value.code in {"patch_invalid_utf8", "patch_binary_file"}
    assert path.read_bytes() == source


@pytest.mark.parametrize("kind", ["before", "after", "exact"])
def test_full_image_byte_bounds(tmp_path, monkeypatch, kind):
    path = tmp_path / "main.py"
    path.write_text("before")
    monkeypatch.setattr(planner, "MAX_PATCH_BYTES", 6)
    with Workspace(tmp_path) as workspace:
        args = proposal(workspace, new="after!" if kind == "exact" else "much longer")
        if kind == "before":
            monkeypatch.setattr(planner, "MAX_PATCH_BYTES", 5)
        if kind == "exact":
            assert prepare_patch(workspace, args, ReadOperation()).after == b"after!"
        else:
            with pytest.raises(KernelError) as error:
                prepare_patch(workspace, args, ReadOperation())
            assert error.value.code == "patch_limit_exceeded"
    assert path.read_text() == "before"


@pytest.mark.parametrize("kind", ["cancel", "timeout"])
def test_operation_stopped_before_io(tmp_path, monkeypatch, kind):
    (tmp_path / "main.py").write_text("before")
    with Workspace(tmp_path) as workspace:
        args = proposal(workspace)
        operation = ReadOperation()
        if kind == "cancel":
            operation.stopped.set()
        else:
            operation.deadline = 0
        monkeypatch.setattr(planner, "_read_image", lambda *args: pytest.fail("不应读取"))
        with pytest.raises(TurnCancelled if kind == "cancel" else KernelError) as error:
            prepare_patch(workspace, args, operation)
        if kind == "timeout":
            assert error.value.code == "patch_timeout"
