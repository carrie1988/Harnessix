"""MCP连接启动与失败结算：强Container先验资源，取消不遗留启动工作。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, cast

from mcp import Client

from harnessix.sandbox.container import ContainerCommandBuilder

DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS = 10.0

if TYPE_CHECKING:
    from harnessix.mcp.runtime import McpClientTarget, McpContainerStdioTarget


async def build_preflight_client(
    target: McpClientTarget,
    stack: AsyncExitStack,
    container_type: type[McpContainerStdioTarget],
) -> Client:
    """仅在固定Container资源有效且原只读工作结算后创建Client。"""

    if isinstance(target, container_type):
        await _verify_container_resources(cast("McpContainerStdioTarget", target).builder)
    return target.build_client(stack)


async def _verify_container_resources(builder: ContainerCommandBuilder) -> None:
    """结算唯一只读Probe后传播父取消，防止延迟启动或重派资源检查。"""

    task = asyncio.create_task(asyncio.to_thread(builder.verify_resource_limits))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        task.exception()
        raise asyncio.CancelledError
    task.result()


async def _close_failed_start(stack: AsyncExitStack, target: McpClientTarget) -> bool:
    cleanup_error = False
    try:
        await asyncio.wait_for(stack.aclose(), timeout=DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        cleanup_error = True
    try:
        await asyncio.wait_for(target.cleanup(), timeout=DEFAULT_MCP_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        cleanup_error = True
    return cleanup_error
