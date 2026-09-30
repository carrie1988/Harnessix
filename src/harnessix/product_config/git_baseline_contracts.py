"""产品Git基准只冻结原始版本与完整观察，不授予写入或会话访问权。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.delivery.contracts import MAX_TRANSACTION_FILES, DeliveryContract
from harnessix.delivery.git_contracts import validate_git_branch_ref
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.workspace_patch_source_contracts import ProductGitDeliverySource
from harnessix.tools.contracts import Revision


class GitBaselineMember(DeliveryContract):
    """原首before的Git树位置；不存在时不伪造空blob。"""

    path: str = Field(min_length=1, max_length=4096)
    oid: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    mode: Literal["100644", "100755"] | None = None

    @model_validator(mode="after")
    def paired_identity(self) -> Self:
        if (self.oid is None) != (self.mode is None):
            raise ValueError("Git基准存在性与对象模式不一致")
        return self


class ProductGitDeliveryBaseline(DeliveryContract):
    """完整原来源、HEAD与Index逻辑观察；摘要不是MAC或批准凭证。"""

    spec_version: Literal["harnessix.product-git-baseline/v1"] = "harnessix.product-git-baseline/v1"
    source: ProductGitDeliverySource
    head_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_tree_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_ref: str = Field(min_length=1, max_length=1024)
    index_observation_sha256: Revision
    index_observation_bytes: int = Field(ge=0, le=8 * 1024 * 1024)
    status_sha256: Revision
    config_names_sha256: Revision
    reader_binding: Revision
    members: tuple[GitBaselineMember, ...] = Field(min_length=1, max_length=MAX_TRANSACTION_FILES)
    digest: Revision

    @model_validator(mode="after")
    def complete_baseline(self) -> Self:
        if self.head_ref != "HEAD":
            validate_git_branch_ref(self.head_ref)
        if (
            len(self.head_oid) != len(self.head_tree_oid)
            or [m.path for m in self.members] != [m.path for m in self.source.mutations]
            or any(m.oid is not None and len(m.oid) != len(self.head_oid) for m in self.members)
            or any(ord(c) < 32 or ord(c) == 127 for c in self.head_ref)
            or not (self.head_ref == "HEAD" or self.head_ref.startswith("refs/heads/"))
        ):
            raise ValueError("Git基准路径、对象格式或Ref无效")
        for member, mutation in zip(self.members, self.source.mutations, strict=True):
            if (member.oid is None) != (mutation.before.presence == "absent"):
                raise ValueError("Git基准与原before存在性不一致")
            if member.mode is not None and int(member.mode[-3:], 8) != mutation.before.mode:
                raise ValueError("Git基准与原before模式不一致")
        if self.digest != product_git_baseline_digest(self):
            raise ValueError("Git基准摘要不一致")
        return self


def product_git_baseline_digest(baseline: ProductGitDeliveryBaseline) -> str:
    """覆盖所有原来源和观察字段，不包含自身摘要。"""
    return canonical_digest(baseline.model_dump(mode="json", exclude={"digest"}, warnings="error"))
