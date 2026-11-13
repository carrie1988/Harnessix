"""Workspace事务补建的父目录：从已保存的计划推导清单，回滚后清理仍为空的目录（ADR 0137）。

目录不是事务成员，计划合同里没有它的位置；“这笔事务会补建哪些目录”只从计划来源快照推导：
规划时观察为缺失、且位于某个新建文件之上的父目录。审批Diff与回滚清理读的是同一份清单。

非职责：不创建目录（发布新建文件成员时由filesystem补建），不删除非空目录或清单之外的目录。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceTransactionPlan
from harnessix.delivery.filesystem import _parent
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import Workspace
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2


def _checkpoint() -> None:
    """清单只读已落盘的计划与父目录历史，沿用调用方的整体期限。"""


def planned_new_directories(
    plan: WorkspaceTransactionPlan, read_blob: Callable[[str], bytes]
) -> tuple[str, ...]:
    """返回该计划发布时要补建的父目录，由浅到深；计划不新建文件或父目录都已存在时为空。"""
    created = [item.path for item in plan.mutations if item.before.presence == "absent"]
    if not created:
        return ()
    parents = (
        read_workspace_parent_closure(plan.source, read_blob, checkpoint=_checkpoint)
        if isinstance(plan.source, WorkspaceSnapshotV2)
        else plan.source.resources
    )
    return tuple(
        sorted(
            item.path
            for item in parents
            if item.location == "workspace"
            and item.access == "read"
            and item.kind == "missing"
            and any(path.startswith(f"{item.path}/") for path in created)
        )
    )


def remove_empty_directories(root: Path, directories: Sequence[str]) -> None:
    """由深到浅删除仍为空的目录；非空、已不存在或已不是目录的保留原样，不报错。

    rmdir只删除空目录且不跟随符号链接；父目录链仍逐段no-follow打开。清理是尽力而为：
    调用方的事务已经发布，留下一个空目录不改变任何文件内容。
    """
    if not directories:
        return
    if os.name == "nt":
        # Windows事务端口自ADR 0137第2版同样补建父目录，回滚时按句柄清理（Beta Issue #16）。
        from harnessix.delivery.windows_filesystem import remove_empty_windows_directories

        remove_empty_windows_directories(root, directories)
        return
    with Workspace(root, path_max_bytes=4096, path_max_parts=128) as workspace:
        for directory in sorted(directories, reverse=True):
            try:
                with _parent(workspace, directory) as (parent, name):
                    os.rmdir(name, dir_fd=parent)
                    os.fsync(parent)
            except (OSError, KernelError, ReadToolError):
                continue
