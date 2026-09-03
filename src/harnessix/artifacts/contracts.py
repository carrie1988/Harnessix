from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from harnessix.agent.models import ToolResultContent
from harnessix.domain.models import ContractModel

if TYPE_CHECKING:
    from harnessix.artifacts.ports import ArtifactPublisher

MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_ARTIFACT_RECORDS = 10000
MAX_PAGE_BYTES = 24 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ArtifactContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactPolicy(ArtifactContract):
    ttl_seconds: int = Field(default=86400, ge=60, le=604800, strict=True)
    max_turn_bytes: int = Field(
        default=4 * MAX_ARTIFACT_BYTES, ge=1, le=32 * MAX_ARTIFACT_BYTES, strict=True
    )
    max_turn_count: int = Field(default=128, ge=1, le=1000, strict=True)
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
class CollectionReport:
    examined: int
    expired: int
    protected: int
    collected_at: datetime
    next_after: UUID | None
