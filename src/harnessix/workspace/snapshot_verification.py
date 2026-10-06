"""严格按实际快照代际复核；新历史缺少宿主端口时不得降级。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.tools.workspace import ReadOperation
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.snapshot import verify_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.snapshot_v2 import verify_workspace_snapshot_v2


def verify_host_workspace_snapshot(
    expected: WorkspaceSnapshot | WorkspaceSnapshotV2,
    workspace_root: Callable[[str], Path],
    ports: WorkspaceSnapshotPorts | None,
) -> None:
    """一次复核共用原读取期限；端口只读验证，不捕获写入或修复历史。"""
    root = workspace_root(expected.workspace_id)
    if isinstance(expected, WorkspaceSnapshotV2):
        if ports is None:
            raise KernelError("workspace_closure_unavailable", "完整Workspace历史端口不可用")
        verify_workspace_snapshot_v2(
            expected, root, checkpoint=ReadOperation().checkpoint, read_blob=ports.read_blob
        )
    else:
        verify_workspace_snapshot(expected, root)
