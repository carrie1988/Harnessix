"""Harnessix会话库转逐步记录：真实运行一个Turn，模型调用与工具调用都有真实时间，原库不被改动。"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from evalkit.contracts import ArmTrials
from evalkit.contracts import Usage as StepUsage
from evalkit.errors import EvalkitError
from evalkit.files import read_model, write_model
from evalkit.harbor import import_harbor_job
from evalkit.steps import Step, StepTrace

from harnessix.agent.errors import AgentFailure
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
from harnessix.product_config import server as product
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.protocol.contracts import PublicBudget, PublicEvent
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from integrations.harbor.protocol_runner import drive_protocol_trial
from integrations.harnessix_eval import harbor_parser
from integrations.harnessix_eval.harbor_parser import (
    EVENTS_ARTIFACT,
    EVENTS_FILE,
    STATE_ARTIFACT,
    STATE_FILE,
)
from integrations.harnessix_eval.steps import (
    export_protocol_steps,
    protocol_turn_steps,
    state_compactions,
    step_trace,
)
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.context.test_compaction_runtime import accounted_text, run_seeded
from tests.evalkit.builders import Trial, harbor_job, harbor_plan
from tests.integrations.test_harnessix_eval import PARSERS, native
from tests.product_config.conftest import product_config, write_config


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


# ---------------------------------------------------------------- 正文：新增输入、回复与压缩


async def test_protocol_generations_carry_new_input_and_reply(tmp_path: Path) -> None:
    _, events, _ = await _protocol_turn(tmp_path)
    first, second = (s for s in protocol_turn_steps(events, model="qwen") if s.kind == "generation")
    assert first.input_text == "[user_message]\nread"
    assert first.output_text == "[tool_call test.read]\n{}"
    tool = protocol_tool(protocol_turn_steps(events, model="qwen"))
    assert second.input_text == f"[tool_result test.read]\n{tool.output_text}"
    assert second.output_text == "[assistant_message]\ndone"


async def test_compactions_are_billed_steps_and_a_summary_is_new_input(tmp_path: Path) -> None:
    """压缩记录来自真实的压缩运行；成功的摘要计入其后那次调用的输入，失败的压缩记原因。"""
    _, events, _ = await _protocol_turn(tmp_path / "turn")
    summary = ScriptedProvider([accounted_text("保留public接口；解析器待验证。")])
    _, _, seeded = await run_seeded(tmp_path / "seeded", summary)
    record = seeded.compactions[0]
    assert record.status == "summarized" and record.summary is not None
    summarized = record.model_copy(
        update={"plan": record.plan.model_copy(update={"model_step": 2})}
    )
    failed = record.model_copy(
        update={
            "plan": record.plan.model_copy(update={"model_step": 1, "compaction_id": uuid4()}),
            "status": "failed",
            "summary": None,
            "failure": AgentFailure(code="provider_summary_truncated", message="摘要达到上限"),
        }
    )
    steps = protocol_turn_steps(events, model="qwen", compactions=(failed, summarized))
    StepTrace(plan_digest="sha256:" + "a" * 64, slot=0, steps=steps)
    compactions = {s.metadata["step"]: s for s in steps if s.name == "compaction"}
    assert compactions["1"].status == "error"
    assert compactions["1"].output_text == "[failure]\nprovider_summary_truncated: 摘要达到上限"
    assert compactions["2"].status == "ok"
    # 压缩摘要正文带宿主标注（ADR 0058第2版§2.7），逐步记录如实导出模型看到的摘要。
    stamped = (
        "[Summary of earlier turns, written before step 1; later messages are newer and "
        "override it.]\n保留public接口；解析器待验证。"
    )
    assert compactions["2"].output_text == "[summary]\n" + stamped
    assert compactions["2"].usage == StepUsage(
        completeness="complete", model_calls=1, input_tokens=10, output_tokens=3
    )
    first, second = (s for s in steps if s.name == "model")
    assert "[context_compacted]" not in (first.input_text or "")
    assert second.input_text.endswith("[context_compacted]\n" + stamped)


async def test_harbor_state_archive_feeds_steps_through_its_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实产品状态目录（含独立密钥）按适配器方式打包，导入登记后由逐步记录只读打开。"""
    monkeypatch.setenv("PRIMARY_API_KEY", "steps-primary-fixture")
    monkeypatch.setenv("BACKUP_API_KEY", "steps-secondary-fixture")
    workspace, trial_id = tmp_path / "workspace", uuid4()
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    events: list[PublicEvent] = []
    receipts: list[dict] = []

    async def build(snapshot, selection, secrets, *, audit):
        return await build_provider_bundle(
            snapshot,
            selection,
            secrets,
            audit=audit,
            factory=lambda *_: ScriptedProvider([answer("done")]),
        )

    async def drive(server, _input, _output):
        client = AgentClient(InProcessAgentTransport(server))
        try:
            receipts.append(
                await drive_protocol_trial(
                    client,
                    workspace=str(workspace),
                    instruction="say done",
                    budget=PublicBudget(
                        max_steps=4,
                        max_tokens=50_000,
                        timeout_seconds=5,
                        max_output_chars=4096,
                        max_tool_calls_per_step=4,
                    ),
                    trial_id=trial_id,
                    events=events,
                )
            )
        finally:
            await client.close()

    monkeypatch.setattr(product, "build_provider_bundle", build)
    monkeypatch.setattr(product, "run_stdio", drive)
    await product.run_product_stdio(
        config_path=write_config(tmp_path / "config.json", product_config()),
        profile_id=None,
        workspace=workspace,
        state_directory=state_root / str(trial_id),
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    (receipt,) = receipts
    assert receipt["reason"] == "completed"

    run_dir = tmp_path / "run"
    metadata = native(thread_id=receipt["thread_id"], turn_id=receipt["turn_id"])
    job = harbor_job(
        run_dir / "job", [Trial(reward=1.0, metadata=metadata)], agent_name="harnessix"
    )
    agent = next(job.glob("trial-*")) / "agent"
    agent.mkdir(exist_ok=True)
    (agent / EVENTS_FILE).write_text("".join(e.model_dump_json() + "\n" for e in events))
    with tarfile.open(agent / STATE_FILE, "w:gz") as archive:  # 同适配器：tar -czf … -C 根 试次ID
        archive.add(state_root / str(trial_id), arcname=str(trial_id))
    plan = harbor_plan(1, system="harnessix")
    trials = import_harbor_job(job, plan, "A", PARSERS, run_dir)
    assert {a.name for a in trials[0].artifacts} >= {EVENTS_ARTIFACT, STATE_ARTIFACT}
    trials_path = run_dir / "trials-A.json"
    write_model(trials_path, ArmTrials(plan_digest=plan.digest, arm_id="A", trials=trials), 1 << 20)

    # export-steps是同步命令（内部自建事件循环读会话库），测试在工作线程中调用。
    exported = await asyncio.to_thread(
        export_protocol_steps, run_dir, trials_path, run_dir / "steps", model="qwen"
    )
    assert exported == 1
    trace = read_model(run_dir / "steps" / "A-slot0.json", StepTrace, 1 << 20)
    (generation,) = trace.steps
    assert generation.input_text == "[user_message]\nsay done"
    assert generation.output_text == "[assistant_message]\ndone"
    body = (agent / STATE_FILE).read_bytes()
    thread_id, turn_id = UUID(receipt["thread_id"]), UUID(receipt["turn_id"])
    assert await asyncio.to_thread(state_compactions, body, thread_id, turn_id) == ()
    with pytest.raises(ValueError, match="没有该Turn"):
        await asyncio.to_thread(state_compactions, body, thread_id, uuid4())


def test_state_archive_is_registered_unless_it_exceeds_the_artifact_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = harbor_job(
        tmp_path / "job", [Trial(reward=1.0, metadata=native())], agent_name="harnessix"
    )
    agent = next(job.glob("trial-*")) / "agent"
    agent.mkdir(exist_ok=True)
    (agent / STATE_FILE).write_bytes(b"x" * 64)
    plan = harbor_plan(1, system="harnessix")
    names = {a.name for a in import_harbor_job(job, plan, "A", PARSERS, tmp_path)[0].artifacts}
    assert STATE_ARTIFACT in names
    monkeypatch.setattr(harbor_parser, "STATE_ARTIFACT_MAX_BYTES", 63)
    names = {a.name for a in import_harbor_job(job, plan, "A", PARSERS, tmp_path)[0].artifacts}
    assert STATE_ARTIFACT not in names
