"""以真实Runtime、Session和公共SDK验证Harbor侧协议驱动，不调用付费模型。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import PublicBudget
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from integrations.harbor import protocol_runner
from integrations.harbor.protocol_runner import drive_protocol_trial
from tests.agent.helpers import RecordingTools, answer, tool_step


def budget(timeout: float = 5.0) -> PublicBudget:
    return PublicBudget(
        max_steps=4,
        max_tokens=1000,
        timeout_seconds=timeout,
        max_output_chars=4096,
        max_tool_calls_per_step=4,
    )


@pytest.mark.parametrize("interactive", [False, True])
async def test_real_runtime_receipt_and_intervention_cancellation(
    tmp_path: Path, interactive: bool
) -> None:
    scripts = (
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="question", tool="ask_user", arguments={"question": "PRIVATE-QUESTION"}
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ]
        ]
        if interactive
        else [answer("PRIVATE-REPLY")]
    )
    provider = ScriptedProvider(scripts)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, enable_questions=True) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        async with client:
            receipt = await drive_protocol_trial(
                client,
                workspace=str(tmp_path),
                instruction="PRIVATE-INSTRUCTION",
                budget=budget(),
                trial_id=uuid4(),
            )
            assert receipt["reason"] == ("intervention_required" if interactive else "completed")
            assert receipt["cost_completeness"] == "unknown"
            assert receipt["turn_terminal_confirmed"]
            assert "PRIVATE-" not in json.dumps(receipt)
            assert len(provider.requests) == 1


@pytest.mark.parametrize("approve", [True, False])
async def test_approval_policy_answers_only_what_it_allows(tmp_path: Path, approve: bool) -> None:
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer("done")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        async with client:
            receipt = await drive_protocol_trial(
                client,
                workspace=str(tmp_path),
                instruction="read",
                budget=budget().model_copy(update={"max_tokens": 50_000}),
                trial_id=uuid4(),
                approval_mode="auto",
                # auto只批准补丁与沙箱内进程；测试工具既不是补丁也不在沙箱前缀内。
                policy=(lambda content, tool: "approved" if tool == "test.read" else None)
                if approve
                else None,
            )
    assert receipt["reason"] == ("completed" if approve else "intervention_required")
    assert receipt["auto_approvals"] == (1 if approve else 0)
    assert receipt["approval_mode"] == "auto"
    assert len(tools.calls) == (1 if approve else 0)


async def test_model_relay_forwards_loopback_port_to_target() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"pong:" + await reader.readline())
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    target = upstream.sockets[0].getsockname()[1]
    (relay,) = await protocol_runner._start_relays([f"127.0.0.1:0=127.0.0.1:{target}"])
    port = relay.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping\n")
        await writer.drain()
        assert await reader.read() == b"pong:ping\n"
        writer.close()
    finally:
        relay.close()
        upstream.close()
    with pytest.raises(ValueError):
        await protocol_runner._start_relays([f"0.0.0.0:0=127.0.0.1:{target}"])


async def test_trial_deadline_covers_initialize_without_restarting_agent() -> None:
    class StalledClient:
        calls = 0

        async def initialize(self):
            self.calls += 1
            await asyncio.Event().wait()

    client = StalledClient()
    receipt = await drive_protocol_trial(
        client, workspace="/app", instruction="task", budget=budget(0.01), trial_id=uuid4()
    )
    assert receipt["reason"] == "deadline_exceeded"
    assert receipt["input_tokens"] is None
    assert client.calls == 1


def test_receipt_reservation_prevents_reexecution(tmp_path: Path, monkeypatch) -> None:
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("task")
    output = tmp_path / "result.json"
    state = tmp_path / "state"
    trial = uuid4()
    run = AsyncMock(return_value={"reason": "completed", "trial_id": str(trial)})
    monkeypatch.setattr(protocol_runner, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--instruction-file",
            str(instruction),
            "--server-config",
            str(tmp_path / "config.json"),
            "--workspace",
            "/app",
            "--state-directory",
            str(state),
            "--output",
            str(output),
            "--trial-id",
            str(trial),
        ],
    )
    with pytest.raises(SystemExit) as first:
        protocol_runner.main()
    assert first.value.code == 0
    before = output.read_bytes()
    with pytest.raises(SystemExit) as duplicate:
        protocol_runner.main()
    assert duplicate.value.code == 1
    assert run.await_count == 1
    assert output.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600


def test_existing_state_is_not_implicitly_restarted(tmp_path: Path, monkeypatch) -> None:
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("task")
    state = tmp_path / "state"
    state.mkdir()
    (state / "existing").write_text("preserve")
    run = AsyncMock()
    monkeypatch.setattr(protocol_runner, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--instruction-file",
            str(instruction),
            "--server-config",
            "/config.json",
            "--workspace",
            "/app",
            "--state-directory",
            str(state),
            "--output",
            str(tmp_path / "result.json"),
            "--trial-id",
            str(uuid4()),
        ],
    )
    with pytest.raises(SystemExit) as failure:
        protocol_runner.main()
    assert failure.value.code == 1
    assert run.await_count == 0
    assert (state / "existing").read_text() == "preserve"
