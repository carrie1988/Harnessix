"""Trusted Workspace Patch：定义模型可提交的严格输入与Review JSONL合同。"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator, model_validator

from harnessix.delivery.contracts import (
    FileMode,
    WorkspaceDiffEntry,
    WorkspaceTransactionPlan,
)
from harnessix.domain.models import ContractModel
from harnessix.tools.contracts import Revision

MAX_WORKSPACE_PATCH_FILES = 16
MAX_WORKSPACE_PATCH_INPUT_BYTES = 512 * 1024
MAX_WORKSPACE_ACTION_REVIEW_BYTES = 1024 * 1024
MAX_REVIEW_CHUNK_CHARACTERS = 3000


class WorkspacePatchFile(ContractModel):
    """一个带来源前置条件的创建、替换或删除意图。"""

    operation: Literal["create", "replace", "delete"]
    path: str = Field(min_length=1, max_length=4096)
    expected_sha256: Revision | None = None
    content: str | None = Field(default=None, max_length=MAX_WORKSPACE_PATCH_INPUT_BYTES)
    mode: FileMode | None = None

    @field_validator("content")
    @classmethod
    def safe_utf8_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            body = value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("Workspace Patch正文必须是合法UTF-8") from None
        if len(body) > MAX_WORKSPACE_PATCH_INPUT_BYTES or any(
            (ord(character) < 32 and character not in "\t\r\n") or ord(character) == 127
            for character in value
        ):
            raise ValueError("Workspace Patch正文超过上限或包含控制字符")
        return value

    @model_validator(mode="after")
    def operation_shape(self) -> Self:
        if self.operation == "create":
            valid = (
                self.expected_sha256 is None and self.content is not None and self.mode is not None
            )
        elif self.operation == "replace":
            valid = (
                self.expected_sha256 is not None
                and self.content is not None
                and self.mode is not None
            )
        else:
            valid = self.expected_sha256 is not None and self.content is None and self.mode is None
        if not valid:
            raise ValueError("Workspace Patch操作字段组合无效")
        return self


class WorkspacePatchInput(ContractModel):
    """模型可见的多文件事务提案；宿主持有事务、租约和Artifact身份。"""

    spec_version: Literal["harnessix.workspace-patch-input/v1"] = (
        "harnessix.workspace-patch-input/v1"
    )
    files: tuple[WorkspacePatchFile, ...] = Field(
        min_length=1,
        max_length=MAX_WORKSPACE_PATCH_FILES,
    )

    @model_validator(mode="after")
    def bounded_unique_files(self) -> Self:
        paths = [item.path for item in self.files]
        total = sum(len((item.content or "").encode("utf-8")) for item in self.files)
        if len(paths) != len(set(paths)):
            raise ValueError("Workspace Patch不能重复定位同一路径")
        if total > MAX_WORKSPACE_PATCH_INPUT_BYTES:
            raise ValueError("Workspace Patch正文总量超过上限")
        return self


class WorkspaceActionReviewSummary(ContractModel):
    """Review首记录；绑定完整Delivery计划和原始Diff摘要。"""

    record_type: Literal["summary"] = "summary"
    spec_version: Literal["harnessix.workspace-action-review/v1"] = (
        "harnessix.workspace-action-review/v1"
    )
    transaction_id: UUID
    plan_fingerprint: Revision
    workspace_revision: Revision
    file_count: int = Field(ge=1, le=MAX_WORKSPACE_PATCH_FILES)
    diff_utf8_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    diff_sha256: Revision
    complete: Literal[True] = True


class WorkspaceActionReviewEntry(ContractModel):
    """Review文件级记录；不重复保存文件正文。"""

    record_type: Literal["entry"] = "entry"
    index: int = Field(ge=0, lt=MAX_WORKSPACE_PATCH_FILES)
    entry: WorkspaceDiffEntry


class WorkspaceActionReviewChunk(ContractModel):
    """Review正文分块；每条记录可由Artifact分页独立读取。"""

    record_type: Literal["text"] = "text"
    sequence: int = Field(ge=0, le=10_000)
    text: str = Field(min_length=1, max_length=MAX_REVIEW_CHUNK_CHARACTERS)


WorkspaceActionReviewRecord = Annotated[
    WorkspaceActionReviewSummary | WorkspaceActionReviewEntry | WorkspaceActionReviewChunk,
    Field(discriminator="record_type"),
]
_RECORD_ADAPTER: TypeAdapter[WorkspaceActionReviewRecord] = TypeAdapter(WorkspaceActionReviewRecord)


class WorkspaceActionReviewDocument(ContractModel):
    """可重建原Diff且满足Artifact单记录和总量边界的JSONL文档。"""

    summary: WorkspaceActionReviewSummary
    entries: tuple[WorkspaceActionReviewEntry, ...]
    chunks: tuple[WorkspaceActionReviewChunk, ...]

    @model_validator(mode="after")
    def complete_document(self) -> Self:
        text = "".join(item.text for item in self.chunks)
        encoded = text.encode("utf-8")
        if (
            len(self.entries) != self.summary.file_count
            or tuple(item.index for item in self.entries) != tuple(range(len(self.entries)))
            or tuple(item.sequence for item in self.chunks) != tuple(range(len(self.chunks)))
            or not self.chunks
            or len(encoded) != self.summary.diff_utf8_bytes
            or hashlib.sha256(encoded).hexdigest() != self.summary.diff_sha256
        ):
            raise ValueError("Workspace Action Review记录不完整")
        return self

    def to_jsonl(self) -> bytes:
        records: tuple[ContractModel, ...] = (self.summary, *self.entries, *self.chunks)
        body = b"".join(
            (
                json.dumps(
                    item.model_dump(mode="json", warnings="error"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            for item in records
        )
        if len(body) > MAX_WORKSPACE_ACTION_REVIEW_BYTES:
            raise ValueError("Workspace Action Review超过Artifact上限")
        return body


def build_workspace_action_review(
    plan: WorkspaceTransactionPlan,
    entries: tuple[WorkspaceDiffEntry, ...],
    text: str,
) -> WorkspaceActionReviewDocument:
    """把完整Diff确定性转换为摘要、文件元数据和可分页正文。"""

    body = text.encode("utf-8", errors="strict")
    summary = WorkspaceActionReviewSummary(
        transaction_id=plan.transaction_id,
        plan_fingerprint=plan.fingerprint,
        workspace_revision=plan.source.revision,
        file_count=len(entries),
        diff_utf8_bytes=len(body),
        diff_sha256=hashlib.sha256(body).hexdigest(),
    )
    document = WorkspaceActionReviewDocument(
        summary=summary,
        entries=tuple(
            WorkspaceActionReviewEntry(index=index, entry=entry)
            for index, entry in enumerate(entries)
        ),
        chunks=tuple(
            WorkspaceActionReviewChunk(sequence=index, text=chunk)
            for index, chunk in enumerate(_chunks(text))
        ),
    )
    # 在交给Artifact Store前执行总量校验，避免创建不可发布的审批请求。
    document.to_jsonl()
    return document


def parse_workspace_action_review(body: bytes) -> WorkspaceActionReviewDocument:
    """严格解析Review JSONL并重新验证记录顺序、正文摘要和总量。"""

    if type(body) is not bytes or len(body) > MAX_WORKSPACE_ACTION_REVIEW_BYTES:
        raise ValueError("Workspace Action Review正文无效")
    try:
        text = body.decode("utf-8", errors="strict")
        if not text.endswith("\n"):
            raise ValueError
        decoded = tuple(_RECORD_ADAPTER.validate_json(line) for line in text.splitlines())
        if not decoded or not isinstance(decoded[0], WorkspaceActionReviewSummary):
            raise ValueError
        entries = tuple(
            item for item in decoded[1:] if isinstance(item, WorkspaceActionReviewEntry)
        )
        chunks = tuple(item for item in decoded[1:] if isinstance(item, WorkspaceActionReviewChunk))
        if decoded != (decoded[0], *entries, *chunks):
            raise ValueError
        document = WorkspaceActionReviewDocument(
            summary=decoded[0],
            entries=entries,
            chunks=chunks,
        )
        if document.to_jsonl() != body:
            raise ValueError
        return document
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("Workspace Action Review JSONL无效") from None


def _chunks(text: str) -> tuple[str, ...]:
    return tuple(
        text[index : index + MAX_REVIEW_CHUNK_CHARACTERS]
        for index in range(0, len(text), MAX_REVIEW_CHUNK_CHARACTERS)
    )
