from dataclasses import replace

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.patches import batches
from harnessix.patches.batch_contracts import (
    MAX_BATCH_FILES,
    MAX_BATCH_IMAGE_BYTES,
    MAX_BATCH_PROPOSAL_BYTES,
    PatchBatchManifest,
    PatchBatchProposal,
)
from harnessix.patches.batches import prepare_patch_batch, validate_patch_batch, verify_patch_batch
from harnessix.patches.contracts import MAX_EDIT_BYTES, ExactEdit, PatchProposal
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.patches.test_planner import proposal


@pytest.fixture
def group(tmp_path):
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_bytes(b"before\r\n")
    with Workspace(tmp_path) as workspace:
        args = PatchBatchProposal(
            files=tuple(proposal(workspace, path=p) for p in ("one.py", "two.py"))
        )
        yield workspace, args


def test_group_plan_reopen_order_binding_and_no_writes(group, tmp_path):
    workspace, args = group
    before = {p.name: p.stat() for p in tmp_path.iterdir()}
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    assert [p.after for p in batch.patches] == [b"after\r\n", b"after\r\n"]
    assert (
        PatchBatchManifest.model_validate_json(batch.manifest.model_dump_json()) == batch.manifest
    )
    assert "before\\r" not in repr(batch) and "after\\r" not in repr(batch)
    reversed_batch = prepare_patch_batch(
        workspace, PatchBatchProposal(files=args.files[::-1]), ReadOperation()
    )
    assert reversed_batch.manifest.fingerprint != batch.manifest.fingerprint
    with Workspace(tmp_path) as reopened:
        verify_patch_batch(reopened, batch, ReadOperation())
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(before)
    for p in tmp_path.iterdir():
        assert p.read_bytes() == b"before\r\n"
        a, b = before[p.name], p.stat()
        assert (a.st_ino, a.st_mtime_ns, a.st_ctime_ns) == (b.st_ino, b.st_mtime_ns, b.st_ctime_ns)


@pytest.mark.parametrize(
    "kind", ["empty", "duplicate", "too_many", "injection", "path", "path_alias"]
)
def test_invalid_group_proposal(group, kind):
    _, args = group
    data = args.model_dump(mode="json")
    if kind == "empty":
        data["files"] = []
    elif kind == "duplicate":
        data["files"] = [data["files"][0]] * 2
    elif kind == "too_many":
        data["files"] = [
            {**data["files"][0], "path": f"{i}.py"} for i in range(MAX_BATCH_FILES + 1)
        ]
    elif kind == "injection":
        data["approved"] = True
    elif kind == "path":
        data["files"][1]["path"] = "../outside"
    else:
        # 不通过路径规范化把不同请求悄悄合并。
        data["files"][1]["path"] = "./one.py"
    import json

    with pytest.raises(ValidationError):
        PatchBatchProposal.model_validate_json(json.dumps(data))


def test_total_proposal_utf8_budget_at_boundary():
    # 每文件两项各128 KiB，两个文件恰好512 KiB；额外一个字节即拒绝。
    old = "中" * (MAX_EDIT_BYTES // 3)  # 131070 字节
    edit = ExactEdit(old_text=old, new_text="xx")
    members = tuple(
        PatchProposal(path=f"{i}.py", expected_revision="0" * 64, edits=(edit, edit))
        for i in range(2)
    )
    assert (
        sum(len(e.old_text.encode()) + len(e.new_text.encode()) for p in members for e in p.edits)
        == MAX_BATCH_PROPOSAL_BYTES
    )
    assert PatchBatchProposal(files=members)
    extra = PatchProposal(
        path="extra.py", expected_revision="0" * 64, edits=(ExactEdit(old_text="x", new_text=""),)
    )
    with pytest.raises(ValidationError):
        PatchBatchProposal(files=(*members, extra))


def test_all_proposals_are_checked_before_any_file_open(group, monkeypatch):
    workspace, args = group

    def forbidden(*args, **kwargs):
        pytest.fail("无效整组提案不能开始文件读取")

    monkeypatch.setattr(workspace, "open", forbidden)
    invalid = args.model_copy(update={"files": (args.files[0], args.files[0])})
    with pytest.raises(ValidationError):
        prepare_patch_batch(workspace, invalid, ReadOperation())


def test_valid_batch_cannot_move_to_other_workspace(group, tmp_path):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    other = tmp_path / "other"
    other.mkdir()
    with Workspace(other) as another:
        with pytest.raises(KernelError) as error:
            validate_patch_batch(another, batch, ReadOperation())
        assert error.value.code == "patch_workspace_changed"


@pytest.mark.parametrize("excess", [False, True])
def test_actual_full_image_budget_boundary(tmp_path, excess):
    # 4 个文件的完整前后镜像正好8 MiB，不依赖伪造 manifest 的小尺寸。
    paths = [f"{i}.py" for i in range(4)]
    for path in paths:
        (tmp_path / path).write_bytes(b"a\n" * (1024 * 1024 // 2 - 1) + b"aX")
    if excess:
        (tmp_path / "extra.py").write_bytes(b"X")
        paths.append("extra.py")
    with Workspace(tmp_path) as workspace:
        args = PatchBatchProposal(
            files=tuple(proposal(workspace, path=p, old="X", new="Y") for p in paths)
        )
        if excess:
            with pytest.raises(KernelError) as error:
                prepare_patch_batch(workspace, args, ReadOperation())
            assert error.value.code == "patch_batch_limit_exceeded"
        else:
            result = prepare_patch_batch(workspace, args, ReadOperation())
            assert (
                sum(len(p.before) + len(p.after) for p in result.patches) == MAX_BATCH_IMAGE_BYTES
            )
    assert all((tmp_path / p).read_bytes().endswith(b"X") for p in paths)


@pytest.mark.parametrize("change", ["content", "deleted", "symlink"])
def test_second_source_failure_does_not_change_first(group, tmp_path, change):
    workspace, args = group
    target = tmp_path / "two.py"
    if change == "content":
        target.write_text("external")
    else:
        target.unlink()
        if change == "symlink":
            target.symlink_to(tmp_path / "one.py")
    with pytest.raises(KernelError):
        prepare_patch_batch(workspace, args, ReadOperation())
    assert (tmp_path / "one.py").read_bytes() == b"before\r\n"


def test_final_whole_group_source_verification(group, tmp_path, monkeypatch):
    workspace, args = group
    original = batches.prepare_patch

    def change_earlier(workspace, proposal, operation):
        result = original(workspace, proposal, operation)
        if proposal.path == "two.py":
            (tmp_path / "one.py").write_bytes(b"external")
        return result

    monkeypatch.setattr(batches, "prepare_patch", change_earlier)
    with pytest.raises(KernelError) as error:
        prepare_patch_batch(workspace, args, ReadOperation())
    assert error.value.code == "patch_source_changed"
    assert (tmp_path / "two.py").read_bytes() == b"before\r\n"


@pytest.mark.parametrize(
    "change", ["members", "proposal", "manifest", "bytes", "scope", "member_type"]
)
def test_private_batch_tampering_rejected(group, change):
    workspace, args = group
    batch = prepare_patch_batch(workspace, args, ReadOperation())
    if change == "members":
        batch = replace(batch, patches=batch.patches[::-1])
    elif change == "proposal":
        batch = replace(batch, proposal=PatchBatchProposal(files=args.files[::-1]))
    elif change == "manifest":
        batch = replace(batch, manifest=batch.manifest.model_copy(update={"fingerprint": "0" * 64}))
    elif change == "bytes":
        batch = replace(
            batch, patches=(replace(batch.patches[0], after=b"forged"), batch.patches[1])
        )
    elif change == "scope":
        batch = replace(
            batch, manifest=batch.manifest.model_copy(update={"workspace_scope": "0" * 64})
        )
    else:
        batch = replace(batch, patches=(object(), batch.patches[1]))
    with pytest.raises(KernelError) as error:
        validate_patch_batch(workspace, batch, ReadOperation())
    assert error.value.code in {"patch_batch_corrupt", "patch_plan_corrupt"}


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_one_operation_budget_covers_whole_batch(group, monkeypatch, mode):
    workspace, args = group
    operation = ReadOperation()
    original = batches.prepare_patch

    def stop(workspace, proposal, operation):
        result = original(workspace, proposal, operation)
        if mode == "cancel":
            operation.stopped.set()
        else:
            operation.deadline = 0
        return result

    monkeypatch.setattr(batches, "prepare_patch", stop)
    with pytest.raises(TurnCancelled if mode == "cancel" else KernelError) as error:
        prepare_patch_batch(workspace, args, operation)
    if mode == "deadline":
        assert error.value.code == "patch_timeout"
