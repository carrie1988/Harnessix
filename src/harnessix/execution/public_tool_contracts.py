"""执行链共享的纯公开正文合同；不导入Executor、Router或扩展装配入口。"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from harnessix.execution.contracts import ExecutionContract, canonical_digest
from harnessix.tools.contracts import Revision

MAX_WORKSPACE_PATCH_FILES = 16
GitObjectId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


def skill_relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Skill路径必须是规范相对路径")
    return value


class PublicWorkspacePatchOutput(ExecutionContract):
    """既有五字段成功摘要；只公开事务身份、成员数、固定终态及Diff摘要。"""

    transaction_id: UUID
    files: int = Field(ge=1, le=MAX_WORKSPACE_PATCH_FILES)
    state: Literal["published"]
    origin: Literal["execution", "recovery"]
    diff_sha256: Revision


# Git Push正式观察收据；自身Hash不替代公开边界的批准意图比对。
class GitPushReceipt(ExecutionContract):
    spec_version: Literal["harnessix.git-push-receipt/v1"] = "harnessix.git-push-receipt/v1"
    push_id: UUID
    remote_name: str
    remote_ref: str
    remote_oid: GitObjectId
    remote_url_sha256: Revision
    observed_at: AwareDatetime
    digest: Revision

    @model_validator(mode="after")
    def complete_receipt(self) -> Self:
        if self.digest != canonical_digest(
            self.model_dump(mode="json", exclude={"digest"}, warnings="error")
        ):
            raise ValueError("Git Push Receipt摘要不一致")
        return self


# MCP公开结果封套；动态Tool Schema和Secret处理仍属于捕获接入点。
class McpToolCallOutput(ExecutionContract):
    spec_version: Literal["harnessix.mcp-tool-call-output/v1"] = "harnessix.mcp-tool-call-output/v1"
    content: tuple[JsonValue, ...] = Field(max_length=256)
    structured_content: JsonValue | None = None
    is_error: bool = False


# 已定位Skill正文及内容摘要；目录和清单绑定由消费者核对。
class SkillContent(ExecutionContract):
    spec_version: Literal["harnessix.skill-content/v1"] = "harnessix.skill-content/v1"
    catalog_sha256: Revision
    manifest_sha256: Revision
    qualified_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$")
    effective_version: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=262_144)
    content_sha256: Revision
    resources: tuple[str, ...] = Field(max_length=64)

    @field_validator("resources")
    @classmethod
    def canonical_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if list(value) != sorted(value) or len(set(value)) != len(value):
            raise ValueError("Skill资源路径必须规范排序且唯一")
        for path in value:
            skill_relative_path(path)
        return value

    @model_validator(mode="after")
    def content_matches_digest(self) -> Self:
        if (
            len(self.content.encode()) > 262_144
            or hashlib.sha256(self.content.encode()).hexdigest() != self.content_sha256
        ):
            raise ValueError("Skill正文摘要不一致")
        return self


# 单个相对路径资源及原文摘要；不授权读取请求之外的路径。
class SkillResourceContent(ExecutionContract):
    spec_version: Literal["harnessix.skill-resource-content/v1"] = (
        "harnessix.skill-resource-content/v1"
    )
    catalog_sha256: Revision
    manifest_sha256: Revision
    qualified_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=65_536)
    content_sha256: Revision

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return skill_relative_path(value)

    @model_validator(mode="after")
    def content_matches_digest(self) -> Self:
        if (
            len(self.content.encode()) > 65_536
            or hashlib.sha256(self.content.encode()).hexdigest() != self.content_sha256
        ):
            raise ValueError("Skill资源摘要不一致")
        return self
