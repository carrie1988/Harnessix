"""Harnessix会话库转逐步记录：真实运行一个Turn，模型调用与工具调用都有真实时间，原库不被改动。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from harnessix.agent.models import Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import ResponseCompleted, ResponseStarted
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.contracts import PublicBudget
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from integrations.harbor.protocol_runner import drive_protocol_trial
from integrations.harnessix_eval.steps import step_trace
from tests.agent.helpers import RecordingTools, answer, tool_step


def attempt(step: int, events: list) -> list:
    """按真实提供方的顺序包上模型调用记录（参照tests/evals/test_campaign_execution.py）。"""
    identity = uuid4()
    usage = Usage(input_tokens=100, output_tokens=10)
    response_id = next(e.response_id for e in events if isinstance(e, ResponseStarted))
    wrapped: list = [
        ModelAttemptStarted(
            attempt_id=identity, step=step, index=1, provider="openai_chat", requested_model="qwen"
        )
    ]
    for event in events:
        if isinstance(event, ResponseCompleted):
            wrapped += [
                ModelUsageObserved(
                    attempt_id=identity,
                    actual_model="qwen-actual",
                    response_id=response_id,
                    usage=UsageObservation(
                        completeness="complete", input_tokens=100, output_tokens=10
                    ),
                ),
                ModelAttemptFinished(attempt_id=identity, outcome="completed"),
                event.model_copy(update={"usage": usage}),
            ]
        else:
            wrapped.append(event)
    return wrapped


async def test_turn_becomes_generation_and_tool_steps(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    store = SQLiteSessionStore(database)
    tools = RecordingTools()
    provider = ScriptedProvider([attempt(1, tool_step("test.read")), attempt(2, answer("done"))])
    async with AgentRuntime(store, provider, tools) as runtime:
        service = AgentApplicationService(runtime, store, SQLiteProtocolRequestStore(store.path))
        async with AgentClient(InProcessAgentTransport(AgentProtocolServer(service))) as client:
            receipt = await drive_protocol_trial(
                client,
                workspace=str(tmp_path),
                instruction="read",
                budget=PublicBudget(
                    max_steps=4,
                    max_tokens=50_000,
                    timeout_seconds=5,
                    max_output_chars=4096,
                    max_tool_calls_per_step=4,
                ),
                trial_id=uuid4(),
            )
    assert receipt["reason"] == "completed"
    before = database.read_bytes()
    trace = await step_trace(
        database,
        UUID(receipt["thread_id"]),
        UUID(receipt["turn_id"]),
        plan_digest="sha256:" + "a" * 64,
        slot=0,
    )
    kinds = [(step.kind, step.name) for step in trace.steps]
    assert kinds.count(("generation", "model")) == 2
    assert ("tool", "test.read") in kinds
    generation = next(step for step in trace.steps if step.kind == "generation")
    assert generation.model == "qwen-actual" and generation.usage.input_tokens == 100
    tool = next(step for step in trace.steps if step.kind == "tool")
    assert tool.status == "ok" and tool.metadata["outcome"] == "succeeded"
    assert all(step.completed_at >= step.started_at for step in trace.steps)
    assert database.read_bytes() == before
