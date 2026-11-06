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
from tests.agent.helpers import answer


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
