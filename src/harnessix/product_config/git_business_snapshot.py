"""从同一轮已认证 GitDB 前缀构建 Backup v2 业务快照。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_business_snapshot_contracts import (
    GitBusinessLinkSnapshot,
    GitDeliveryBusinessSnapshot,
)
from harnessix.product_config.git_decision_link_rows import interpret_git_link_histories
from harnessix.product_config.git_prefix_catalog import GitPrefixCatalog
from harnessix.product_config.git_prefix_rows import GitPrefixRows


def build_git_delivery_business_snapshot(
    capture_id: UUID,
    catalog: GitPrefixCatalog,
    rows: GitPrefixRows,
    *,
    checkpoint: Callable[[], None],
) -> GitDeliveryBusinessSnapshot:
    """解释全部已认证行；当前未实现业务表非空时失败关闭。"""
    checkpoint()
    if type(capture_id) is not UUID:
        raise KernelError("git_business_snapshot_invalid", "Git业务快照捕获身份无效")
    try:
        histories = interpret_git_link_histories(catalog, rows, checkpoint=checkpoint)
    except KernelError as error:
        if error.code == "git_decision_link_scope_unsupported":
            raise KernelError(
                "git_business_snapshot_scope_unsupported", "Git业务快照超出当前正式语义范围"
            ) from None
        raise
    return GitDeliveryBusinessSnapshot.model_validate(
        {
            "capture_id": capture_id,
            "prefix": catalog,
            "links": tuple(
                GitBusinessLinkSnapshot(prepared=item.prepared, decision=item.decision)
                for item in histories
            ),
        },
        strict=True,
        context={"checkpoint": checkpoint},
    )
