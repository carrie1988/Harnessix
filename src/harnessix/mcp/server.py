from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.schema import (
    bounded_mcp_output,
    validate_mcp_arguments,
    validate_mcp_input_schema,
)
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, TrustedToolBinding
from harnessix.trusted_actions.router import ExtensionActionPort


@dataclass(frozen=True, slots=True)
class McpExportedTool:
    public_name: str
    binding_name: str
    description: str
    input_schema: dict[str, JsonValue]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", self.public_name):
            raise KernelError("mcp_export_invalid", "MCP导出Tool名称无效")
        if not self.description or len(self.description) > 16384:
            raise KernelError("mcp_export_invalid", "MCP导出Tool描述无效")
        object.__setattr__(self, "input_schema", validate_mcp_input_schema(self.input_schema))


class HarnessixMcpServer:
    """把显式白名单只读Action导出为本地MCP Server。"""

    def __init__(
        self,
        *,
        port: ExtensionActionPort,
        exports: tuple[McpExportedTool, ...],
        name: str = "Harnessix Code",
        version: str = "0.8.4",
    ) -> None:
        if not exports:
            raise KernelError("mcp_export_invalid", "MCP Server至少需要一个导出Tool")
        if len({item.public_name for item in exports}) != len(exports):
            raise KernelError("mcp_export_invalid", "MCP导出Tool名称重复")
        bindings = {binding.tool: binding for binding in port.bindings()}
        for exported in exports:
            binding = bindings.get(exported.binding_name)
            if binding is None:
                raise KernelError("mcp_export_not_registered", "MCP导出Tool未通过可信Action注册")
            self._validate_binding(binding, exported)
        self._port = port
        self._exports = tuple(sorted(exports, key=lambda item: item.public_name))
        self._bindings = bindings
        self._by_public_name = {item.public_name: item for item in self._exports}
        self._server: Server[dict[str, object]] = Server(
            name,
            version=version,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    @property
    def low_level_server(self) -> Server[dict[str, object]]:
        return self._server

    async def serve_stdio(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )

    async def _list_tools(
        self,
        _: ServerRequestContext[dict[str, object]],
        __: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        current = {binding.tool: binding for binding in self._port.bindings()}
        tools: list[Tool] = []
        for exported in self._exports:
            binding = current.get(exported.binding_name)
            if binding is None:
                continue
            self._validate_binding(binding, exported)
            tools.append(
                Tool(
                    name=exported.public_name,
                    description=exported.description,
                    input_schema=cast(dict[str, Any], exported.input_schema),
                )
            )
        return ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")

    async def _call_tool(
        self,
        _: ServerRequestContext[dict[str, object]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        exported = self._by_public_name.get(params.name)
        if exported is None:
            return _tool_error("mcp_export_not_found")
        binding = next(
            (item for item in self._port.bindings() if item.tool == exported.binding_name),
            None,
        )
        if binding is None:
            return _tool_error("mcp_export_not_registered")
        try:
            self._validate_binding(binding, exported)
            parsed = validate_mcp_arguments(exported.input_schema, params.arguments or {})
            plan = self._port.plan(
                invocation_id=uuid4(),
                tool=binding.tool,
                tool_version=binding.tool_version,
                tool_fingerprint=binding.tool_fingerprint,
                arguments=parsed.root,
            )
            if plan.state != "ready":
                return _tool_error(
                    "mcp_export_approval_required"
                    if plan.state == "pending_approval"
                    else "mcp_export_denied"
                )
            outcome = await self._port.execute(plan.plan.execution.plan_id)
        except KernelError as error:
            return _tool_error(error.code)
        if outcome.kind != "succeeded":
            return _tool_error(outcome.error_code or "mcp_export_execution_failed")
        return _tool_success(outcome)

    @staticmethod
    def _validate_binding(binding: TrustedToolBinding, exported: McpExportedTool) -> None:
        if (
            binding.effect_class is not EffectClass.READ_ONLY
            or binding.risk_level is not RiskLevel.LOW
            or binding.recovery_mode != "none"
            or binding.input_schema_sha256 != canonical_digest(exported.input_schema)
        ):
            raise KernelError(
                "mcp_export_policy_invalid",
                "MCP Server只允许导出低风险只读且Schema一致的Action",
            )


def _tool_success(outcome: ActionExecutionOutcome) -> CallToolResult:
    output = bounded_mcp_output(outcome.output)
    text = json.dumps(
        output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=output,
        is_error=False,
    )


def _tool_error(code: str) -> CallToolResult:
    safe_code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "mcp_export_failed"
    return CallToolResult(
        content=[TextContent(type="text", text=f"Harnessix Tool执行失败：{safe_code}")],
        is_error=True,
    )
