"""MCP工具接入：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from harnessix.execution.contracts import ExecutionContract, canonical_digest
from harnessix.tools.contracts import Revision

MCP_PROTOCOL_VERSION = "2026-07-28"
McpTransportKind = Literal["container_stdio", "in_process", "streamable_http"]
McpConnectionState = Literal[
    "connecting",
    "connected",
    "schema_changed",
    "failed",
    "closed",
]


class McpServerIdentity(ExecutionContract):
    spec_version: Literal["harnessix.mcp-server-identity/v1"] = "harnessix.mcp-server-identity/v1"
    server_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    transport: McpTransportKind
    target_sha256: Revision
    protocol_version: str = Field(min_length=1, max_length=64)
    reported_name: str | None = Field(default=None, max_length=256)
    reported_version: str | None = Field(default=None, max_length=128)
    capabilities_sha256: Revision


class McpToolSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.mcp-tool-snapshot/v1"] = "harnessix.mcp-tool-snapshot/v1"
    raw_name: str = Field(min_length=1, max_length=256)
    model_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    title: str | None = Field(default=None, max_length=1024)
    description: str | None = Field(default=None, max_length=16384)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue] | None = None
    annotations: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    definition_sha256: Revision
    tool_sha256: Revision

    @model_validator(mode="after")
    def valid_digest(self) -> Self:
        if self.tool_sha256 != mcp_tool_snapshot_digest(self):
            raise ValueError("MCP Tool快照摘要不一致")
        return self


def mcp_tool_snapshot_digest(tool: McpToolSnapshot) -> str:
    return canonical_digest(tool.model_dump(mode="json", exclude={"tool_sha256"}, warnings="error"))


class McpCatalogSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.mcp-catalog-snapshot/v1"] = "harnessix.mcp-catalog-snapshot/v1"
    server: McpServerIdentity
    generation: int = Field(ge=1)
    captured_at: AwareDatetime
    tools: tuple[McpToolSnapshot, ...] = Field(max_length=2048)
    catalog_sha256: Revision

    @model_validator(mode="after")
    def canonical_catalog(self) -> Self:
        raw_names = [tool.raw_name for tool in self.tools]
        model_names = [tool.model_name for tool in self.tools]
        if (
            raw_names != sorted(raw_names)
            or len(set(raw_names)) != len(raw_names)
            or len(set(model_names)) != len(model_names)
            or self.catalog_sha256 != mcp_catalog_snapshot_digest(self)
        ):
            raise ValueError("MCP目录快照不规范")
        return self


def mcp_catalog_snapshot_digest(snapshot: McpCatalogSnapshot) -> str:
    return canonical_digest(
        {
            "server": snapshot.server.model_dump(mode="json", warnings="error"),
            "tools": [tool.model_dump(mode="json", warnings="error") for tool in snapshot.tools],
        }
    )


class McpConnectionEvent(ExecutionContract):
    spec_version: Literal["harnessix.mcp-connection-event/v1"] = "harnessix.mcp-connection-event/v1"
    server_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    sequence: int = Field(ge=1)
    from_state: McpConnectionState | None
    to_state: McpConnectionState
    catalog_sha256: Revision | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    previous_digest: Revision | None = None
    occurred_at: AwareDatetime
    digest: Revision

    @model_validator(mode="after")
    def valid_event(self) -> Self:
        if (self.sequence == 1) != (self.previous_digest is None):
            raise ValueError("MCP连接事件前序摘要无效")
        if self.to_state == "connected" and self.catalog_sha256 is None:
            raise ValueError("MCP已连接事件缺少目录摘要")
        if self.to_state in {"failed", "schema_changed"} and self.error_code is None:
            raise ValueError("MCP失败事件缺少错误码")
        if self.to_state not in {"failed", "schema_changed"} and self.error_code is not None:
            raise ValueError("MCP非失败事件不能携带错误码")
        if self.digest != mcp_connection_event_digest(self):
            raise ValueError("MCP连接事件摘要不一致")
        return self


def mcp_connection_event_digest(event: McpConnectionEvent) -> str:
    return canonical_digest(event.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def build_mcp_connection_event(
    *,
    server_id: str,
    sequence: int,
    from_state: McpConnectionState | None,
    to_state: McpConnectionState,
    previous_digest: str | None,
    occurred_at: datetime,
    catalog_sha256: str | None = None,
    error_code: str | None = None,
) -> McpConnectionEvent:
    candidate = McpConnectionEvent.model_construct(
        _fields_set=None,
        server_id=server_id,
        sequence=sequence,
        from_state=from_state,
        to_state=to_state,
        catalog_sha256=catalog_sha256,
        error_code=error_code,
        previous_digest=previous_digest,
        occurred_at=occurred_at,
        digest="0" * 64,
    )
    return McpConnectionEvent(
        **candidate.model_dump(exclude={"digest"}),
        digest=mcp_connection_event_digest(candidate),
    )


class McpConnectionSnapshot(ExecutionContract):
    spec_version: Literal["harnessix.mcp-connection-snapshot/v1"] = (
        "harnessix.mcp-connection-snapshot/v1"
    )
    server_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    state: McpConnectionState
    sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    catalog_sha256: Revision | None = None
    last_event_digest: Revision
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if (self.generation == 0) != (self.catalog_sha256 is None):
            raise ValueError("MCP连接快照目录代次不一致")
        if self.state == "connected" and self.catalog_sha256 is None:
            raise ValueError("MCP已连接快照缺少目录")
        if self.state in {"failed", "schema_changed"} and self.error_code is None:
            raise ValueError("MCP失败快照缺少错误码")
        if self.state not in {"failed", "schema_changed"} and self.error_code is not None:
            raise ValueError("MCP非失败快照不能携带错误码")
        return self


class McpToolCallOutput(ExecutionContract):
    spec_version: Literal["harnessix.mcp-tool-call-output/v1"] = "harnessix.mcp-tool-call-output/v1"
    content: tuple[JsonValue, ...] = Field(max_length=256)
    structured_content: JsonValue | None = None
    is_error: bool = False
