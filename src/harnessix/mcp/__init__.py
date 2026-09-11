"""MCP工具接入：汇总并导出受支持的公共入口，不承载运行时编排。"""

from harnessix.mcp.actions import (
    McpActionGateway,
    McpTrustedToolPolicy,
    build_mcp_action_definition,
    static_mcp_resource_resolver,
)
from harnessix.mcp.contracts import (
    MCP_PROTOCOL_VERSION,
    McpCatalogSnapshot,
    McpConnectionEvent,
    McpConnectionSnapshot,
    McpServerIdentity,
    McpToolCallOutput,
    McpToolSnapshot,
)
from harnessix.mcp.runtime import (
    McpCallAfterSendError,
    McpClientConnection,
    McpContainerStdioTarget,
    McpInProcessTarget,
)
from harnessix.mcp.schema import McpToolArguments, validate_mcp_arguments
from harnessix.mcp.server import HarnessixMcpServer, McpExportedTool
from harnessix.mcp.store import SQLiteMcpStore

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "HarnessixMcpServer",
    "McpActionGateway",
    "McpCallAfterSendError",
    "McpCatalogSnapshot",
    "McpClientConnection",
    "McpConnectionEvent",
    "McpConnectionSnapshot",
    "McpContainerStdioTarget",
    "McpExportedTool",
    "McpInProcessTarget",
    "McpServerIdentity",
    "McpToolArguments",
    "McpToolCallOutput",
    "McpToolSnapshot",
    "McpTrustedToolPolicy",
    "SQLiteMcpStore",
    "build_mcp_action_definition",
    "static_mcp_resource_resolver",
    "validate_mcp_arguments",
]
