"""在原私有CAS中规划完整父目录历史，不把派生父项计为显式叶。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import workspace_transaction_plan_fingerprint
from harnessix.delivery.planner import (
    DesiredWorkspaceFile,
    PreparedWorkspaceTransaction,
    _native_platform,
    _normalized_targets,
    _prepare_mutations,
)
from harnessix.delivery.workspace_v2_contracts import WorkspaceTransactionPlanV2
from harnessix.workspace.contracts import PlatformKind, WorkspaceResourceRequest
from harnessix.workspace.snapshot_v2 import (
    capture_workspace_snapshot_v2,
    verify_workspace_snapshot_v2,
)


def prepare_workspace_transaction_v2(
    root: str | Path,
    desired: Mapping[str, DesiredWorkspaceFile],
    *,
    request_id: str,
    checkpoint: Callable[[], None],
    write_blob: Callable[[str, bytes], None],
    read_blob: Callable[[str], bytes],
    transaction_id: UUID | None = None,
    now: datetime | None = None,
    platform: PlatformKind | None = None,
) -> PreparedWorkspaceTransaction:
    """完整捕获、共享镜像读取及只读复核；不保存业务事务或批准。"""
    checkpoint()
    selected = platform or _native_platform()
    checked_now = now or datetime.now(UTC)
    if checked_now.tzinfo is None or not 1 <= len(request_id) <= 128 or not desired:
        raise KernelError("delivery_plan_invalid", "Workspace事务规划参数无效")
    normalized = _normalized_targets(desired, selected)
    snapshot = capture_workspace_snapshot_v2(
        root,
        resources=tuple(WorkspaceResourceRequest(path=path, access="write") for path in normalized),
        platform=selected,
        checkpoint=checkpoint,
        write_blob=write_blob,
        read_blob=read_blob,
    )
    observations = {
        (item.path, item.access): item
        for item in snapshot.resources
        if item.location == "workspace"
    }
    mutations, blobs = _prepare_mutations(
        Path(root), normalized, observations, selected, checkpoint=checkpoint
    )
    verify_workspace_snapshot_v2(snapshot, root, checkpoint=checkpoint, read_blob=read_blob)
    candidate = WorkspaceTransactionPlanV2.model_construct(
        transaction_id=transaction_id or uuid4(),
        request_id=request_id,
        source=snapshot,
        mutations=mutations,
        created_at=checked_now,
        fingerprint="0" * 64,
    )
    # 仅用构造对象计算新代际摘要；对外结果必须再次经过完整领域校验。
    plan = WorkspaceTransactionPlanV2(
        **candidate.model_dump(exclude={"fingerprint"}, warnings="error"),
        fingerprint=workspace_transaction_plan_fingerprint(candidate),
    )
    checkpoint()
    return PreparedWorkspaceTransaction(plan=plan, blobs=MappingProxyType(blobs))
