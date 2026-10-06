"""完整Git审阅合同：保留真实调用和全部Diff，不虚构Workspace事务。"""

from __future__ import annotations

import hashlib
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, ValidationInfo, model_validator

from harnessix.agent.models import ToolCallContent
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILES,
    MAX_WORKSPACE_DIFF_BYTES,
    DeliveryContract,
    WorkspaceDiffEntry,
)
from harnessix.delivery.trusted_action_contracts import WorkspaceActionReviewChunk
from harnessix.tools.contracts import Revision


class ProductGitActionReviewSummary(DeliveryContract):
    """独立Git首记录，完整用户意图与Core内容地址不可省略。"""

    record_type: Literal["summary"] = "summary"
    spec_version: Literal["harnessix.product-git-action-review/v1"] = (
        "harnessix.product-git-action-review/v1"
    )
    core_fingerprint: Revision
    delivery_id: UUID
    call: ToolCallContent = Field(repr=False)
    workspace_revision: Revision
    base_commit_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    file_count: int = Field(ge=1, le=MAX_TRANSACTION_FILES)
    diff_utf8_bytes: int = Field(ge=1, le=MAX_WORKSPACE_DIFF_BYTES)
    diff_sha256: Revision
    complete: Literal[True] = True


class ProductGitActionReviewEntry(DeliveryContract):
    """Git完整文件记录；不能误用公共Patch的16文件界限。"""

    record_type: Literal["entry"] = "entry"
    index: int = Field(ge=0, lt=MAX_TRANSACTION_FILES)
    entry: WorkspaceDiffEntry


class ProductGitActionReviewDocument(DeliveryContract):
    """完整连续记录重建原Diff；原Diff SHA与Artifact JSONL SHA各自独立。"""

    summary: ProductGitActionReviewSummary
    entries: tuple[ProductGitActionReviewEntry, ...] = Field(max_length=MAX_TRANSACTION_FILES)
    chunks: tuple[WorkspaceActionReviewChunk, ...] = Field(
        min_length=1, max_length=10_000, repr=False
    )

    @model_validator(mode="after")
    def complete_document(self, info: ValidationInfo) -> Self:
        """字段、连续序号与完整UTF8摘要共同验真，不签发来源认证或执行批准。"""
        check = info.context["checkpoint"] if info.context else lambda: None
        check()
        if len(self.entries) != self.summary.file_count:
            raise ValueError("Git Review记录不完整")
        for index, entry in enumerate(self.entries):
            check()
            if entry.index != index:
                raise ValueError("Git Review记录不连续")
        digest, size = hashlib.sha256(), 0
        for index, chunk in enumerate(self.chunks):
            check()
            if chunk.sequence != index:
                raise ValueError("Git Review记录不连续")
            body = chunk.text.encode("utf-8", "strict")
            digest.update(body)
            size += len(body)
        if (size, digest.hexdigest()) != (self.summary.diff_utf8_bytes, self.summary.diff_sha256):
            raise ValueError("Git Review全文摘要不一致")
        check()
        return self
