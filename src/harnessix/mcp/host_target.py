"""本机stdio MCP目标（ADR 0126）：以用户本人权限启动，只接受已解析的绝对命令与最小环境。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from mcp import Client, StdioServerParameters, stdio_client

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.contracts import McpTransportKind
from harnessix.mcp.runtime import (
    DEFAULT_MCP_CALL_TIMEOUT_SECONDS,
    DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS,
    _validate_server_id,
    _validate_timeout,
)


@dataclass(frozen=True, slots=True)
class McpHostStdioTarget:
    """用户已信任的本机Server；目标摘要即信任指纹，定义变化后不能复用旧目录与绑定。"""

    server_id: str
    command: Path
    arguments: tuple[str, ...]
    working_directory: Path
    definition_sha256: str
    environment: Mapping[str, str] = field(repr=False)
    secret_values: tuple[str, ...] = field(default=(), repr=False)
    startup_timeout_seconds: float = DEFAULT_MCP_STARTUP_TIMEOUT_SECONDS
    call_timeout_seconds: float = DEFAULT_MCP_CALL_TIMEOUT_SECONDS
    transport: McpTransportKind = "host_stdio"

    def __post_init__(self) -> None:
        _validate_server_id(self.server_id)
        if not self.command.is_absolute() or not self.working_directory.is_absolute():
            raise KernelError("mcp_target_invalid", "MCP本机命令与工作目录必须是绝对路径")
        if len(self.definition_sha256) != 64:
            raise KernelError("mcp_target_invalid", "MCP本机定义摘要无效")
        _validate_timeout(self.startup_timeout_seconds, "mcp_startup_timeout_invalid")
        _validate_timeout(self.call_timeout_seconds, "mcp_call_timeout_invalid")

    @property
    def target_sha256(self) -> str:
        return canonical_digest(
            {
                "kind": self.transport,
                "server_id": self.server_id,
                "definition": self.definition_sha256,
            }
        )

    @property
    def sandbox_profile_digest(self) -> None:
        return None

    @property
    def sandbox_network(self) -> None:
        return None

    def build_client(self, stack: AsyncExitStack) -> Client:
        # Server的stderr常含调试信息，可能带出配置值；丢弃而不转给终端或日志。
        stderr = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
        parameters = StdioServerParameters(
            command=str(self.command),
            args=list(self.arguments),
            env=dict(self.environment),
            cwd=self.working_directory,
        )
        return Client(
            stdio_client(parameters, errlog=stderr),
            mode="auto",
            read_timeout_seconds=self.call_timeout_seconds,
            cache=None,
        )

    async def cleanup(self) -> None:
        # SDK退出stdio上下文时已终止子进程（Windows经Job Object回收进程树）。
        return None

    def redaction_values(self) -> tuple[bytes, ...]:
        return tuple(value.encode() for value in self.secret_values if value)
