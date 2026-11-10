"""本机stdio MCP测试Server（ADR 0127）：自称只读的回显工具，以及在工作目录写标记文件的工具。

启动时把PID写入参数指定的文件，测试据此确认会话结束后进程已退出。
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

TOOLS = [
    Tool(
        name="echo",
        description="Echo text back. Read-only, never needs approval.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False),
    ),
    Tool(
        name="write_marker",
        description="Write a marker file in the working directory.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
]


async def list_tools(
    _: ServerRequestContext[dict[str, object]],
    __: PaginatedRequestParams | None,
) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def call_tool(
    _: ServerRequestContext[dict[str, object]],
    params: CallToolRequestParams,
) -> CallToolResult:
    arguments = params.arguments or {}
    if params.name == "write_marker":
        target = Path(str(arguments["name"]))
        await asyncio.to_thread(target.write_text, os.environ.get("FIXTURE_TOKEN", "none"))
        return CallToolResult(content=[TextContent(type="text", text="written")])
    return CallToolResult(content=[TextContent(type="text", text=f"echo:{arguments['text']}")])


async def main(pid_file: Path) -> None:
    await asyncio.to_thread(pid_file.write_text, str(os.getpid()), encoding="ascii")
    server: Server[dict[str, object]] = Server(
        "host-fixture",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pid_file", type=Path)
    asyncio.run(main(parser.parse_args().pid_file))
