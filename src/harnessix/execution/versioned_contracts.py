"""完整父目录历史的执行契约；保留旧执行代际及其字节合同。"""

from __future__ import annotations

from typing import Literal

from harnessix.execution.contracts import ExecutionPlanV2
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2


class ExecutionPlanV3(ExecutionPlanV2):
    spec_version: Literal["harnessix.execution-plan/v3"] = "harnessix.execution-plan/v3"  # type: ignore[assignment]
    workspace: WorkspaceSnapshotV2  # type: ignore[assignment]
