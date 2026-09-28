from __future__ import annotations

import asyncio
import subprocess
import threading
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.mcp.runtime import McpClientConnection, McpContainerStdioTarget
from harnessix.mcp.store import SQLiteMcpStore
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.sandbox.container import ContainerCommandBuilder, PreparedContainerLaunch
from harnessix.sandbox.planner import build_container_execution
from tests.sandbox.test_container import _fixtures


def _target(root: Path, inspect) -> McpContainerStdioTarget:
    root.mkdir()
    engine, probe, profile, command, plan = _fixtures(root)
    process = build_process_spec(invocation="argv", argv=command.argv)
    execution = build_container_execution(command, process, owner_capability_digest="f" * 64)
    builder = ContainerCommandBuilder(engine, probe, inspect_runner=inspect)
    prepared = PreparedContainerLaunch(
        argv=(str(engine), "run", profile.image),
        base_environment={},
        secrets=None,
        plan_fingerprint=plan.fingerprint,
        profile_digest=profile.digest,
        execution_digest=execution.digest,
        container_name=builder.container_name(execution),
    )
    return McpContainerStdioTarget("resource-admission", prepared, execution, profile, builder)


@pytest.mark.parametrize(
    "failure", ["missing", "timeout", "nonzero", "invalid", "unsupported_type"]
)
@pytest.mark.parametrize("declared_transport", ["container_stdio", "in_process"])
async def test_mcp_prepared_target_rechecks_resources_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, declared_transport: str
) -> None:
    calls = Counter()

    def inspect(argv, timeout):
        calls[argv[1]] += 1
        if argv[1] != "info":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert timeout == 15.0
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        output = {
            "missing": b"[false,true,true,true]",
            "nonzero": b"[true,true,true,true]",
            "invalid": b"{}",
            "unsupported_type": None,
        }[failure]
        return subprocess.CompletedProcess(
            argv, int(failure == "nonzero"), output, b"private-diagnostic-sentinel"
        )

    def unexpected_client(*args, **kwargs):
        pytest.fail("资源拒绝后不得创建MCP Client")

    monkeypatch.setattr(McpContainerStdioTarget, "build_client", unexpected_client)
    target = _target(tmp_path / "workspace", inspect)
    target = replace(target, transport=declared_transport)  # type: ignore[arg-type]
    with SQLiteMcpStore(tmp_path / "state/mcp.db") as store:
        with pytest.raises(KernelError) as error:
            await McpClientConnection.connect(target, store)
        assert error.value.code == "sandbox_resources_unavailable"
        assert "private-diagnostic-sentinel" not in str(error.value)
        assert store.load(target.server_id).error_code == "sandbox_resources_unavailable"
        assert calls == {"info": 1, "container": 1}


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_mcp_parent_cancellation_settles_original_resource_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_fails: bool
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = Counter()

    def inspect(argv, timeout):
        calls[argv[1]] += 1
        if argv[1] != "info":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        entered.set()
        assert release.wait(5)
        finished.set()
        output = b"[false,true,true,true]" if worker_fails else b"[true,true,true,true]"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    def unexpected_client(*args, **kwargs):
        pytest.fail("取消结算后不得创建MCP Client")

    monkeypatch.setattr(McpContainerStdioTarget, "build_client", unexpected_client)
    target = _target(tmp_path / "workspace", inspect)
    with SQLiteMcpStore(tmp_path / "state/mcp.db") as store:
        task = asyncio.create_task(McpClientConnection.connect(target, store))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert finished.is_set()
        assert calls == {"info": 1, "container": 1}
        assert store.load(target.server_id).error_code == "mcp_startup_cancelled"
