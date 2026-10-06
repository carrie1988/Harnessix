"""完整父历史的Git来源与基准代际；复用原校验，不截断新Snapshot字段。"""

from __future__ import annotations

from typing import Literal

from harnessix.product_config.git_baseline_contracts import ProductGitDeliveryBaseline
from harnessix.product_config.workspace_patch_source_contracts import ProductGitDeliverySource
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2


class ProductGitDeliverySourceV2(ProductGitDeliverySource):
    """当前全路径观察及完整父引用；不包含Git写入权限或新批准。"""

    spec_version: Literal["harnessix.product-git-delivery-source/v2"] = (
        "harnessix.product-git-delivery-source/v2"  # type: ignore[assignment]
    )
    workspace: WorkspaceSnapshotV2  # type: ignore[assignment]


class ProductGitDeliveryBaselineV2(ProductGitDeliveryBaseline):
    """完整Source2绑定固定Git观察；沿用原对象、模式和全字段摘要校验。"""

    spec_version: Literal["harnessix.product-git-baseline/v2"] = "harnessix.product-git-baseline/v2"  # type: ignore[assignment]
    source: ProductGitDeliverySourceV2
