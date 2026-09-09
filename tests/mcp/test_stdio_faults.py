from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters, stdio_client

from harnessix.execution.contracts import canonical_digest
from harnessix.mcp import (
    McpCallAfterSendError,
    McpClientConnection,
    SQLiteMcpStore,
)


@dataclass(frozen=True, slots=True)
class _StdioTarget:
    server_id: str
    script: Path
    pid_file: Path
    startup_timeout_seconds: float = 5
    call_timeout_seconds: float = 0.2
    transport: str = "in_process"
    sandbox_profile_digest: None = None
    sandbox_network: None = None

    @property
    def target_sha256(self) -> str:
        return canonical_digest(
            {
                "python": sys.executable,
                "script": str(self.script),
                "script_sha256": canonical_digest(self.script.read_bytes().hex()),
            }
        )

    def build_client(self, stack: AsyncExitStack) -> Client:
        error_log = stack.enter_context(open(self.pid_file.with_suffix(".stderr"), "w"))
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(self.script), str(self.pid_file)],
            cwd=self.script.parents[3],
        )
        return Client(
            stdio_client(params, errlog=error_log),
            cache=None,
            read_timeout_seconds=self.call_timeout_seconds,
        )

    async def cleanup(self) -> None:
        return None

    def redaction_values(self) -> tuple[bytes, ...]:
        return ()


def target(tmp_path: Path, *, timeout: float = 0.2) -> _StdioTarget:
    return _StdioTarget(
        server_id="fault-server",
        script=Path(__file__).parent / "fixtures/fault_server.py",
        pid_file=tmp_path / "pid",
        call_timeout_seconds=timeout,
    )


async def wait_process_gone(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"MCP child process {pid} remained alive")


async def test_real_stdio_server_crash_settles_call_and_process(tmp_path: Path) -> None:
    selected = target(tmp_path)
    store = SQLiteMcpStore(tmp_path / "mcp.db")
    connection = await McpClientConnection.connect(selected, store)  # type: ignore[arg-type]
    pid = int(selected.pid_file.read_text(encoding="ascii"))
    tool = connection.tool("crash")

    with pytest.raises(McpCallAfterSendError) as caught:
        await connection.call(
            expected_catalog_sha256=connection.catalog.catalog_sha256,
            expected_tool_sha256=tool.tool_sha256,
            raw_name="crash",
            arguments={},
        )
    assert caught.value.code == "mcp_tool_connection_lost"
    assert store.load("fault-server").state == "failed"

    await connection.aclose()
    await wait_process_gone(pid)
    assert store.load("fault-server").state == "closed"


async def test_real_stdio_tool_timeout_is_bounded_and_close_kills_child(
    tmp_path: Path,
) -> None:
    selected = target(tmp_path, timeout=0.1)
    store = SQLiteMcpStore(tmp_path / "mcp.db")
    connection = await McpClientConnection.connect(selected, store)  # type: ignore[arg-type]
    pid = int(selected.pid_file.read_text(encoding="ascii"))
    tool = connection.tool("hang")

    with pytest.raises(McpCallAfterSendError) as caught:
        await connection.call(
            expected_catalog_sha256=connection.catalog.catalog_sha256,
            expected_tool_sha256=tool.tool_sha256,
            raw_name="hang",
            arguments={},
        )
    assert caught.value.code == "mcp_tool_timeout"
    assert store.load("fault-server").state == "connected"

    await connection.aclose()
    await wait_process_gone(pid)
