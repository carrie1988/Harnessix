"""产品Patch与逆向事务共用实际来源代际的规划分派，不推导执行批准。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from harnessix.delivery.planner import (
    DesiredWorkspaceFile,
    PreparedWorkspaceTransaction,
    prepare_workspace_transaction,
)
from harnessix.delivery.planner_v2 import prepare_workspace_transaction_v2
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.tools.workspace import ReadOperation
from harnessix.trusted_actions.contracts import ActionRoutePlan
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2


def prepare_action_workspace_transaction(
    route: ActionRoutePlan,
    root: Path,
    desired: Mapping[str, DesiredWorkspaceFile],
    transactions: SQLiteWorkspaceTransactionStore,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> PreparedWorkspaceTransaction:
    """两个产品动作复用相同CAS、来源及请求身份；调用方仍须核对完整Route。"""
    operation = ReadOperation()

    def check() -> None:
        if checkpoint is not None:
            checkpoint()
        operation.checkpoint()

    if isinstance(route.execution.workspace, WorkspaceSnapshotV2):
        return prepare_workspace_transaction_v2(
            root,
            desired,
            request_id=f"action:{route.execution.plan_id}",
            transaction_id=route.execution.plan_id,
            platform=route.execution.workspace.platform,
            checkpoint=check,
            write_blob=transactions.put_blob,
            read_blob=transactions.blob,
        )
    return prepare_workspace_transaction(
        root,
        desired,
        request_id=f"action:{route.execution.plan_id}",
        transaction_id=route.execution.plan_id,
        platform=route.execution.workspace.platform,
    )
