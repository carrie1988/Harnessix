"""事务Artifact存储：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from harnessix.domain.artifact_pagination import (
    MAX_ARTIFACT_BYTES as MAX_ARTIFACT_BYTES,
)
from harnessix.domain.artifact_pagination import (
    MAX_ARTIFACT_RECORDS as MAX_ARTIFACT_RECORDS,
)
from harnessix.domain.artifact_pagination import (
    MAX_PAGE_BYTES as MAX_PAGE_BYTES,
)
from harnessix.domain.models import ContractModel

if TYPE_CHECKING:
    from harnessix.agent.models import ToolResultContent
    from harnessix.artifacts.ports import ArtifactPublisher

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type HistoryArtifactPurpose = Literal[
    "tool_result",
    "batch_effect",
    "process_output",
    "action_review",
    "action_output",
    "artifact_page",
]
type ArtifactOmittedField = Literal["preview", "matches", "paths"]

# 存储配额不足时结果照常提交、不带完整存档：公开输出用这个键代替"artifact"，
# 告诉模型与客户端完整正文没有保存（ADR 0163）。
ARTIFACT_OMITTED_KEY = "artifact_omitted"
ARTIFACT_STORAGE_FULL = "storage_full"


class ArtifactContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactPolicy(ArtifactContract):
    # 单Turn默认配额取允许范围的上限：长任务每步约产生0.8～0.9件（命令输出与审批Review），
    # 单件最大1 MiB；128件/4 MiB会在约150步或五六次大输出时中断仍在步数预算内的Turn。
    # 存储总量仍由max_live_bytes与max_manifests约束。
    ttl_seconds: int = Field(default=86400, ge=60, le=604800, strict=True)
    max_turn_bytes: int = Field(
        default=32 * MAX_ARTIFACT_BYTES, ge=1, le=32 * MAX_ARTIFACT_BYTES, strict=True
    )
    max_turn_count: int = Field(default=1000, ge=1, le=1000, strict=True)
    max_live_bytes: int = Field(
        default=32 * MAX_ARTIFACT_BYTES, ge=1, le=256 * MAX_ARTIFACT_BYTES, strict=True
    )
    max_manifests: int = Field(default=10000, ge=1, le=100000, strict=True)


class ArtifactRef(ArtifactContract):
    artifact_id: UUID
    sha256: Digest
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    records: int = Field(ge=0, le=MAX_ARTIFACT_RECORDS)
    format: Literal["jsonl/v1"] = "jsonl/v1"
    complete: bool
    expires_at: AwareDatetime


class ReadArtifactInput(ArtifactContract):
    artifact_id: UUID
    offset: int = Field(default=0, ge=0, le=MAX_ARTIFACT_RECORDS, strict=True)
    limit: int = Field(default=100, ge=1, le=200, strict=True)


class ArtifactPage(ArtifactContract):
    artifact: ArtifactRef
    offset: int = Field(ge=0, le=MAX_ARTIFACT_RECORDS)
    text: str
    next_offset: int | None

    @model_validator(mode="after")
    def valid_page(self) -> Self:
        count = self.text.count("\n")
        end = self.offset + count
        if (
            len(self.text.encode()) > MAX_PAGE_BYTES
            or (self.text and not self.text.endswith("\n"))
            or count > 200
            or end > self.artifact.records
            or (
                self.next_offset is not None
                and (count == 0 or self.next_offset != end or end >= self.artifact.records)
            )
            or (self.next_offset is None and end != self.artifact.records)
        ):
            raise ValueError("Artifact 分页范围或字节数不一致")
        return self


@dataclass(frozen=True, slots=True)
class ArtifactToolResult:
    """仅宿主端口可返回；body 是完整记录流，而非模型可见正文。"""

    result: ToolResultContent
    body: bytes
    workspace_scope: str
    complete: bool
    publisher: ArtifactPublisher = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HistoryReferenceCheck:
    """单条模型历史Artifact引用的批量验证输入；字段与逐条实参一一对应。"""

    owner_thread_id: UUID
    call_id: UUID
    reference: ArtifactRef
    purpose: HistoryArtifactPurpose
    omitted_field: ArtifactOmittedField | None = None


@dataclass(frozen=True, slots=True)
class CollectionReport:
    examined: int
    expired: int
    protected: int
    collected_at: datetime
    next_after: UUID | None


def artifact_store_contract(policy: ArtifactPolicy) -> dict[str, object]:
    """纯存储能力合同；运行Epoch不改变既有Tool指纹或公开ArtifactRef。"""
    return {
        "version": "sqlite-artifact/v1",
        "policy": policy.model_dump(mode="json"),
        "max_bytes": MAX_ARTIFACT_BYTES,
        "max_records": MAX_ARTIFACT_RECORDS,
        "page_bytes": MAX_PAGE_BYTES,
        "reference": ArtifactRef.model_json_schema(),
        "page": ArtifactPage.model_json_schema(),
    }
