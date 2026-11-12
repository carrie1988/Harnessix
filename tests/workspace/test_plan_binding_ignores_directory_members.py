"""计划快照对目录只绑定“是哪个目录、权限是什么”，不绑定目录里有哪些成员（ADR 0143）。

真实复测（0.10.0b20，vLLM仓库）：审批窗口打开的111秒里，另一个会话往同一仓库写了日志文件，批准后
计划被判为过期，获批的测试文件没有写入。无关成员的增减不应让已批准的计划失效；计划真正依赖的事实
（目标文件、缺失目标、父目录对象与权限）变化时仍然失效。两代快照共用同一原生观察，逐项都验证。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot
from harnessix.workspace.snapshot_v2 import (
    capture_workspace_snapshot_v2,
    verify_workspace_snapshot_v2,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="用POSIX文件操作构造场景")

EDIT = WorkspaceResourceRequest(path="pkg/sub/app.py", access="write")
CREATE = WorkspaceResourceRequest(path="pkg/sub/new.py", access="write")
PARENTS = tuple(
    WorkspaceResourceRequest(path=path, access="read") for path in (".", "pkg", "pkg/sub")
)
Verify = Callable[[], None]


@pytest.fixture(params=["first_generation", "second_generation"])
def approved(request: pytest.FixtureRequest, tmp_path: Path):
    """建好工作区并捕获一份计划快照；返回工作区根与“执行前复核”函数。"""
    root = tmp_path / "workspace"
    (root / "pkg/sub").mkdir(parents=True)
    (root / "pkg/sub/app.py").write_text("before\n", encoding="utf-8")
    (root / "pkg/sub/other.py").write_text("other\n", encoding="utf-8")
    if request.param == "first_generation":
        # 第1代把父目录作为显式read资源。
        snapshot = capture_workspace_snapshot(root, cwd="pkg", resources=(EDIT, CREATE, *PARENTS))
        yield root, lambda: verify_workspace_snapshot(snapshot, root)
        return
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        second = capture_workspace_snapshot_v2(
            root,
            cwd="pkg",
            resources=(EDIT, CREATE),
            checkpoint=lambda: None,
            write_blob=store.put_blob,
            read_blob=store.blob,
        )
        yield (
            root,
            lambda: verify_workspace_snapshot_v2(
                second, root, checkpoint=lambda: None, read_blob=store.blob
            ),
        )


def _stale(verify: Verify) -> None:
    with pytest.raises(KernelError) as caught:
        verify()
    assert caught.value.code == "execution_plan_stale"


def test_unrelated_members_appearing_or_disappearing_keep_the_plan_valid(approved) -> None:
    root, verify = approved
    verify()
    # 工作区根、中间目录、目标所在目录、cwd里各多出文件和子目录；再删掉一个原有的无关文件。
    (root / "editor-swap.tmp").write_text("x", encoding="utf-8")
    (root / ".pytest_cache").mkdir()
    (root / "pkg/stress.log").write_text("log\n", encoding="utf-8")
    (root / "pkg/__pycache__").mkdir()
    (root / "pkg/sub/notes.txt").write_text("notes\n", encoding="utf-8")
    (root / "pkg/sub/other.py").unlink()

    verify()

    # 无关文件的内容变化同样不在计划之内。
    (root / "pkg/stress.log").write_text("more\n", encoding="utf-8")
    verify()


def test_a_changed_target_file_still_invalidates_the_plan(approved) -> None:
    root, verify = approved
    (root / "pkg/sub/app.py").write_text("edited elsewhere\n", encoding="utf-8")
    _stale(verify)


def test_a_file_appearing_at_the_path_to_create_still_invalidates_the_plan(approved) -> None:
    root, verify = approved
    (root / "pkg/sub/new.py").write_text("someone else\n", encoding="utf-8")
    _stale(verify)


def test_a_case_variant_at_the_path_to_create_still_invalidates_the_plan(approved) -> None:
    root, verify = approved
    if not (root / "PKG").exists():
        pytest.skip("文件系统区分大小写：不同大小写是两个互不相干的文件")
    # 大小写不敏感的文件系统上，NEW.py与待新建的new.py是同一个路径。
    (root / "pkg/sub/NEW.py").write_text("someone else\n", encoding="utf-8")
    _stale(verify)


def test_a_replaced_parent_directory_still_invalidates_the_plan(approved) -> None:
    root, verify = approved
    # 同名但已经是另一个目录：里面的文件内容一模一样也不行。
    (root / "pkg/sub").rename(root / "pkg/moved")
    (root / "pkg/sub").mkdir()
    (root / "pkg/sub/app.py").write_text("before\n", encoding="utf-8")
    _stale(verify)


def test_changed_parent_permissions_still_invalidate_the_plan(approved) -> None:
    root, verify = approved
    (root / "pkg").chmod(0o700 if (root / "pkg").stat().st_mode & 0o077 else 0o755)
    _stale(verify)


def test_a_target_below_missing_directories_follows_the_same_rule(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "pkg").mkdir(parents=True)
    target = WorkspaceResourceRequest(path="pkg/new/deep/mod.py", access="write")
    snapshot = capture_workspace_snapshot(root, resources=(target,))

    # 最近的已有目录里多出无关成员：计划仍然有效。
    (root / "pkg/unrelated.txt").write_text("x", encoding="utf-8")
    (root / "pkg/unrelated-dir").mkdir()
    assert verify_workspace_snapshot(snapshot, root) == snapshot

    # 要补建的目录被别人先建出来了：计划依赖的事实变了。
    (root / "pkg/new").mkdir()
    with pytest.raises(KernelError) as caught:
        verify_workspace_snapshot(snapshot, root)
    assert caught.value.code == "execution_plan_stale"


def test_directory_observations_record_no_member_count(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "pkg").mkdir(parents=True)
    for index in range(3):
        (root / "pkg" / f"file{index}.txt").write_text("x", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        root, resources=(WorkspaceResourceRequest(path="pkg", access="read"),)
    )
    sizes = {item.path: item.size for item in snapshot.resources}
    assert sizes == {".": 0, "pkg": 0}
