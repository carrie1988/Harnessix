"""Git来源按实际快照代际复核，完整父历史只读使用原Workspace CAS。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from harnessix.tools.workspace import ReadOperation
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.snapshot import verify_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_v2 import verify_workspace_snapshot_v2


def verify_git_workspace_snapshot(
    expected: WorkspaceSnapshot | WorkspaceSnapshotV2,
    root: str | Path,
    *,
    read_blob: Callable[[str], bytes],
) -> None:
    """新历史与当前Native共用原读取期限；旧快照保持原复核及失败语义。"""
    if isinstance(expected, WorkspaceSnapshotV2):
        verify_workspace_snapshot_v2(
            expected, root, checkpoint=ReadOperation().checkpoint, read_blob=read_blob
        )
    else:
        verify_workspace_snapshot(expected, root)
