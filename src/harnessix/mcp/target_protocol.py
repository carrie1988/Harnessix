"""MCP客户端目标的统一接口：容器、本机stdio与进程内目标都满足它。"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Protocol

from mcp import Client

from harnessix.mcp.contracts import McpTransportKind


class McpClientTarget(Protocol):
    """各目标都是不可变的：成员只读，属性或冻结字段均可满足。"""

    @property
    def server_id(self) -> str: ...
    @property
    def transport(self) -> McpTransportKind: ...
    @property
    def target_sha256(self) -> str: ...
    @property
    def startup_timeout_seconds(self) -> float: ...
    @property
    def call_timeout_seconds(self) -> float: ...
    @property
    def sandbox_profile_digest(self) -> str | None: ...
    @property
    def sandbox_network(self) -> str | None: ...

    def build_client(self, stack: AsyncExitStack) -> Client: ...

    async def cleanup(self) -> None: ...

    def redaction_values(self) -> tuple[bytes, ...]: ...
