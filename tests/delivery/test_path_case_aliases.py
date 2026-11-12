"""大小写不敏感文件系统上的路径别名（ADR 0068第2版，用户2026-11-08决定开源前修复）。

缺陷：POSIX路径按字节比较，macOS默认APFS却不区分大小写。已有a.txt时补丁写A.txt会被规划为修改
a.txt；同一批同时写a.txt与A.txt会规划成两个成员，第二个成员发布时发现文件已变，留下部分效果。
修复：规划前按文件系统的实际语义识别别名，整批明确拒绝；已有文件以磁盘上的名称为准。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.patches.contracts import ExactEdit
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step
from tests.product_config.test_product_patch_rollback import product, result

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX文件系统语义")
CONFLICT = "delivery_path_case_conflict"


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "harnessix-case-probe"
    probe.write_bytes(b"")
    try:
        return (directory / "HARNESSIX-CASE-PROBE").exists()
    finally:
        probe.unlink()


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/a.txt").write_bytes(b"old\n")
    return root


def _plan(root: Path, desired: dict[str, DesiredWorkspaceFile], generation: int, state: Path):
    if generation == 1:
        return prepare_workspace_transaction(root, desired, request_id="case").plan
    with SQLiteWorkspaceTransactionStore(state) as store:
        return prepare_workspace_transaction_v2(
            root,
            desired,
            request_id="case",
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store.blob,
        ).plan


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


NEW = DesiredWorkspaceFile(b"new\n", 0o644)


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize(
    ("desired", "real"),
    [
        ({"src/A.txt": NEW}, "src/a.txt"),
        ({"Src/a.txt": NEW}, "src/a.txt"),
        ({"src/A.txt": DesiredWorkspaceFile(None)}, "src/a.txt"),
    ],
    ids=["leaf", "parent", "delete"],
)
def test_existing_file_written_with_other_case_is_rejected(tmp_path, generation, desired, real):
    """现象一：已有src/a.txt时补丁写A.txt；规划必须拒绝并给出磁盘上的真实路径。"""
    root = _workspace(tmp_path)
    if not _case_insensitive(root):
        pytest.skip("文件系统区分大小写")
    before = _snapshot(root)
    with pytest.raises(KernelError) as rejected:
        _plan(root, desired, generation, tmp_path / "state")
    assert rejected.value.code == CONFLICT
    assert real in rejected.value.message and next(iter(desired)) in rejected.value.message
    assert _snapshot(root) == before


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("existing", [True, False], ids=["existing", "both-new"])
def test_one_batch_cannot_name_the_same_file_twice(tmp_path, generation, existing):
    """现象二：同一批同时写a.txt与A.txt；无论文件是否已存在，都整批拒绝。"""
    root = _workspace(tmp_path)
    if not _case_insensitive(root):
        pytest.skip("文件系统区分大小写")
    if not existing:
        (root / "src/a.txt").unlink()
    desired = {"src/a.txt": NEW, "src/A.txt": DesiredWorkspaceFile(b"other\n", 0o644)}
    before = _snapshot(root)
    with pytest.raises(KernelError) as rejected:
        _plan(root, desired, generation, tmp_path / "state")
    assert rejected.value.code == CONFLICT
    assert "src/a.txt" in rejected.value.message and "src/A.txt" in rejected.value.message
    assert _snapshot(root) == before


@pytest.mark.parametrize("generation", [1, 2])
def test_case_sensitive_file_systems_keep_distinct_names(tmp_path, generation):
    """区分大小写的文件系统（一般的Linux）行为不变：a.txt与A.txt是两个文件。"""
    root = _workspace(tmp_path)
    if _case_insensitive(root):
        pytest.skip("文件系统不区分大小写")
    desired = {"src/a.txt": NEW, "src/A.txt": DesiredWorkspaceFile(b"other\n", 0o644)}
    plan = _plan(root, desired, generation, tmp_path / "state")
    assert [(m.path, m.before.presence) for m in plan.mutations] == [
        ("src/A.txt", "absent"),
        ("src/a.txt", "file"),
    ]


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize(
    "paths",
    [("pkg/a.txt", "pkg/A.txt"), ("pkg/a.txt", "PKG/a.txt"), ("Pkg", "pkg/a.txt")],
    ids=["leaf", "new-directory", "file-and-directory"],
)
def test_aliases_below_a_directory_that_does_not_exist_yet_are_rejected(
    tmp_path, generation, paths
):
    """父目录由事务补建后（ADR 0137）同样的缺陷会出现在新目录里：规划前按已有目录的语义整批拒绝。"""
    root = _workspace(tmp_path)
    if not _case_insensitive(root):
        pytest.skip("文件系统区分大小写")
    before = _snapshot(root)
    with pytest.raises(KernelError) as rejected:
        _plan(root, dict.fromkeys(paths, NEW), generation, tmp_path / "state")
    assert rejected.value.code == CONFLICT
    assert all(path in rejected.value.message for path in paths)
    assert _snapshot(root) == before and not (root / "pkg").exists()


@pytest.mark.parametrize("generation", [1, 2])
def test_case_sensitive_file_systems_keep_distinct_new_directories(tmp_path, generation):
    root = _workspace(tmp_path)
    if _case_insensitive(root):
        pytest.skip("文件系统不区分大小写")
    plan = _plan(root, {"pkg/a.txt": NEW, "PKG/a.txt": NEW}, generation, tmp_path / "state")
    assert [m.path for m in plan.mutations] == ["PKG/a.txt", "pkg/a.txt"]


@pytest.mark.parametrize("generation", [1, 2])
@pytest.mark.parametrize("parent", ["pkg", "src/pkg"], ids=["root", "existing-directory"])
def test_one_batch_cannot_use_a_path_as_both_a_file_and_a_new_directory(
    tmp_path, generation, parent
):
    """任何文件系统上都成立：同一批里一项新建文件pkg，另一项又要在pkg/下新建文件。"""
    root = _workspace(tmp_path)
    desired = {parent: NEW, f"{parent}/a.txt": NEW}
    before = _snapshot(root)
    with pytest.raises(KernelError) as rejected:
        _plan(root, desired, generation, tmp_path / "state")
    assert rejected.value.code == CONFLICT
    assert all(path in rejected.value.message for path in desired)
    assert _snapshot(root) == before and not (root / parent).exists()


def test_sibling_whose_name_extends_a_file_name_is_not_a_conflict(tmp_path):
    root = _workspace(tmp_path)
    plan = prepare_workspace_transaction(
        root, {"pkg": NEW, "pkgs/a.txt": NEW, "pkg.d/a.txt": NEW}, request_id="siblings"
    ).plan
    assert [m.path for m in plan.mutations] == ["pkg", "pkg.d/a.txt", "pkgs/a.txt"]


def test_exact_disk_names_plan_normally(tmp_path):
    root = _workspace(tmp_path)
    (root / "src/README.md").write_bytes(b"readme\n")
    plan = prepare_workspace_transaction(
        root,
        {"src/a.txt": NEW, "src/README.md": DesiredWorkspaceFile(b"x\n", 0o644)},
        request_id="exact",
    ).plan
    assert [m.path for m in plan.mutations] == ["src/README.md", "src/a.txt"]


async def test_patch_with_wrong_case_is_correctable_feedback(tmp_path):
    """产品链路：拒绝是可修正的失败结果，工作区与事务账本都没有变化，模型可改用真实名称。"""
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        if not _case_insensitive(root):
            pytest.skip("文件系统区分大小写")
        before = _snapshot(root)
        item = WorkspacePatchFile(
            operation="edit",
            path="src/Modified.py",
            expected_sha256=hashlib.sha256(b"old\n").hexdigest(),
            edits=(ExactEdit(old_text="old", new_text="new"),),
        )
        provider.steps = (
            _action_step(WorkspacePatchInput(files=(item,))),
            answer("已处理"),
        )
        thread = await runtime.create_thread(str(root))
        turn = await runtime.run_turn(thread.thread_id, "修改文件", request_id="case")
        output = result(turn)
        assert turn.status is TurnStatus.COMPLETED
        assert output.outcome == "failed" and output.error.code == CONFLICT
        assert "src/modified.py" in output.error.message
        assert output.error.message.endswith("本次Patch未执行，工作区没有变化")
        assert _snapshot(root) == before
        assert transactions._db.execute(
            "SELECT count(*) FROM workspace_transactions"
        ).fetchone() == (0,)
