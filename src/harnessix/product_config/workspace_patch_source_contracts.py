"""产品修改来源：冻结原成功调用引用与Git交付的当前版本投影，不签发执行授权。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILES,
    MAX_TRANSACTION_IMAGE_BYTES,
    DeliveryContract,
    WorkspaceMutation,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import WorkspaceSnapshot
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key


class WorkspacePatchSourceReference(DeliveryContract):
    """原会话调用及两条计划身份；引用本身不能替代Session认证或新批准。"""

    turn_id: UUID
    call_id: UUID
    transaction_id: UUID
    route_fingerprint: Revision
    transaction_fingerprint: Revision


class ProductGitDeliverySource(DeliveryContract):
    """只读投影的净变化与当前Snapshot；未绑定Git HEAD、对象或Ref。"""

    spec_version: Literal["harnessix.product-git-delivery-source/v1"] = (
        "harnessix.product-git-delivery-source/v1"
    )
    thread_id: UUID
    patches: tuple[WorkspacePatchSourceReference, ...] = Field(min_length=1, max_length=256)
    workspace: WorkspaceSnapshot
    mutations: tuple[WorkspaceMutation, ...] = Field(min_length=1, max_length=MAX_TRANSACTION_FILES)
    digest: Revision

    @model_validator(mode="after")
    def complete_source(self) -> Self:
        identities = [item.transaction_id for item in self.patches]
        paths = [item.path for item in self.mutations]
        normalized = [normalize_workspace_path(path, self.workspace.platform) for path in paths]
        keys = [path_comparison_key(path, self.workspace.platform) for path in paths]
        observed = {
            item.path: item
            for item in self.workspace.resources
            if item.location == "workspace" and item.access == "read" and item.path != "."
        }
        if (
            len(identities) != len(set(identities))
            or paths != normalized
            or keys != sorted(set(keys))
            or not set(paths) <= observed.keys()
            or sum(item.before.size + item.after.size for item in self.mutations)
            > MAX_TRANSACTION_IMAGE_BYTES
        ):
            raise ValueError("Git交付来源身份、路径或容量无效")
        for mutation in self.mutations:
            observation = observed[mutation.path]
            after = mutation.after
            expected_kind = "missing" if after.presence == "absent" else "file"
            if (observation.kind, observation.content_sha256, observation.size) != (
                expected_kind,
                after.sha256,
                after.size,
            ):
                raise ValueError("Git交付来源最终版本与Snapshot不一致")
        if self.digest != product_git_delivery_source_digest(self):
            raise ValueError("Git交付来源摘要不一致")
        return self


def product_git_delivery_source_digest(source: ProductGitDeliverySource) -> str:
    return canonical_digest(source.model_dump(mode="json", exclude={"digest"}, warnings="error"))
