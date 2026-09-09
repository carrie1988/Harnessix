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
)

TOOLS = [
    Tool(name=name, input_schema={"type": "object", "additionalProperties": False})
    for name in ("crash", "hang")
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
    if params.name == "crash":
        os._exit(23)
    if params.name == "hang":
        await asyncio.sleep(60)
    return CallToolResult(content=[TextContent(type="text", text="done")])


async def main(pid_file: Path) -> None:
    await asyncio.to_thread(pid_file.write_text, str(os.getpid()), encoding="ascii")
    server: Server[dict[str, object]] = Server(
        "fault-server",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("pid_file", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.pid_file))
