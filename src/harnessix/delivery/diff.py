"""Workspace Diff身份门面；唯一内容算法由纯编码器共享，旧表示与字段保持。"""

from __future__ import annotations

from harnessix.delivery.contracts import WorkspaceDiffDocument, WorkspaceTransactionPlan
from harnessix.delivery.diff_content import build_diff_content
from harnessix.delivery.parent_directories import planned_new_directories
from harnessix.delivery.preserved_mode_contracts import (
    WorkspaceDiffDocumentV2,
    preserves_file_modes,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore


def _checkpoint() -> None:
    """旧同步门面没有取消参数；产品调用边界仍由原宿主结算。"""


def build_workspace_diff(
    plan: WorkspaceTransactionPlan,
    store: SQLiteWorkspaceTransactionStore,
) -> WorkspaceDiffDocument:
    """保留原事务身份与完整Document校验，不借新Git表示改写历史Review。"""

    content = build_diff_content(
        plan.mutations,
        store.blob,
        max_utf8_bytes=None,
        checkpoint=_checkpoint,
        missing_newline_markers=False,
        new_directories=planned_new_directories(plan, store.blob),
    )
    document_type: type[WorkspaceDiffDocument] = (
        WorkspaceDiffDocumentV2 if preserves_file_modes(plan) else WorkspaceDiffDocument
    )
    return document_type(
        transaction_id=plan.transaction_id,
        plan_fingerprint=plan.fingerprint,
        entries=content.entries,
        text=content.text,
        utf8_bytes=content.utf8_bytes,
        sha256=content.sha256,
    )
