"""Harnessix会话库转逐步记录：真实运行一个Turn，模型调用与工具调用都有真实时间，原库不被改动。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from evalkit.contracts import ArmTrials
from evalkit.contracts import Usage as StepUsage
from evalkit.errors import EvalkitError
from evalkit.files import read_model, write_model
from evalkit.harbor import import_harbor_job
from evalkit.steps import Step, StepTrace

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
from harnessix.protocol.contracts import PublicBudget, PublicEvent
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from integrations.harbor.protocol_runner import drive_protocol_trial
from integrations.harnessix_eval.harbor_parser import EVENTS_ARTIFACT, EVENTS_FILE
from integrations.harnessix_eval.steps import export_protocol_steps, protocol_turn_steps, step_trace
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.evalkit.builders import Trial, harbor_job, harbor_plan
from tests.integrations.test_harnessix_eval import PARSERS, native


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


async def _protocol_turn(tmp_path: Path) -> tuple[dict, list[PublicEvent], Path]:
    database = tmp_path / "sessions.db"
    store = SQLiteSessionStore(database)
    provider = ScriptedProvider([attempt(1, tool_step("test.read")), attempt(2, answer("done"))])
    events: list[PublicEvent] = []
    async with AgentRuntime(store, provider, RecordingTools()) as runtime:
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
                events=events,
            )
    assert receipt["reason"] == "completed"
    return receipt, events, database


async def test_protocol_events_match_session_tool_windows_and_bound_generations(
    tmp_path: Path,
) -> None:
    receipt, events, database = await _protocol_turn(tmp_path)
    assert events and all(event.turn_id == UUID(receipt["turn_id"]) for event in events)
    protocol = protocol_turn_steps(events, model="qwen")
    session = await step_trace(
        database,
        UUID(receipt["thread_id"]),
        UUID(receipt["turn_id"]),
        plan_digest="sha256:" + "a" * 64,
        slot=0,
    )

    def tools(steps: tuple[Step, ...]) -> list[tuple]:
        return [
            (s.step_id, s.name, s.started_at, s.completed_at, s.status)
            for s in steps
            if s.kind == "tool"
        ]

    # 工具窗口与会话库同源：同一组item_finished事件。
    assert tools(protocol) == tools(session.steps) and len(tools(protocol)) == 1
    generations = [s for s in protocol if s.kind == "generation"]
    actual = [s for s in session.steps if s.kind == "generation"]
    assert len(generations) == len(actual) == 2
    assert generations[0].usage == StepUsage(
        completeness="partial", model_calls=1, input_tokens=100, output_tokens=10
    )
    assert generations[0].model == "qwen"
    assert generations[0].metadata == {"step": "1", "window": "protocol_events"}
    # 第二次调用从工具结果完成开始；协议窗口覆盖真实调用，不缩短。
    assert generations[1].started_at == protocol_tool(protocol).completed_at
    for bounded, real in zip(generations, actual, strict=True):
        assert bounded.started_at <= real.started_at
        assert bounded.completed_at >= real.completed_at


def protocol_tool(steps: tuple[Step, ...]) -> Step:
    return next(step for step in steps if step.kind == "tool")


async def test_exported_events_become_step_traces_bound_to_artifact_digest(
    tmp_path: Path,
) -> None:
    receipt, events, _ = await _protocol_turn(tmp_path / "turn")
    run_dir = tmp_path / "run"
    job = harbor_job(
        run_dir / "job", [Trial(reward=1.0, metadata=native())], agent_name="harnessix"
    )
    agent = next(job.glob("trial-*")) / "agent"
    agent.mkdir(exist_ok=True)
    body = "".join(event.model_dump_json() + "\n" for event in events)
    (agent / EVENTS_FILE).write_text(body)
    plan = harbor_plan(1, system="harnessix")
    trials = import_harbor_job(job, plan, "A", PARSERS, run_dir)
    assert {a.name for a in trials[0].artifacts} >= {EVENTS_ARTIFACT}
    trials_path = run_dir / "trials-A.json"
    write_model(trials_path, ArmTrials(plan_digest=plan.digest, arm_id="A", trials=trials), 1 << 20)

    assert export_protocol_steps(run_dir, trials_path, run_dir / "steps", model="qwen") == 1
    trace = read_model(run_dir / "steps" / "A-slot0.json", StepTrace, 1 << 20)
    assert trace.plan_digest == plan.digest and trace.slot == 0
    assert [s.kind for s in trace.steps].count("generation") == 2

    (agent / EVENTS_FILE).write_text(body.replace('"read"', '"edit"', 1))
    with pytest.raises(EvalkitError, match="摘要"):
        export_protocol_steps(run_dir, trials_path, run_dir / "steps-again", model="qwen")
