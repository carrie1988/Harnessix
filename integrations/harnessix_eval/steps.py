"""Harnessix → evalkit逐步记录，两个来源：

- 会话库：模型调用来自Turn的ModelAttempt（真实起止时间、实际模型、逐次用量）；工具调用来自事件流中
  tool_call与tool_result两次ItemFinished的发生时间。原库先复制再读取，证据不被改动。
- 公开协议事件（Harbor试次由协议运行器导出）：模型调用结束于该步用量记录（响应完成），开始取上一步
  工具结果全部完成的时刻（首步为Turn开始），与mini-swe-agent相邻消息窗口同一口径；协议不公开请求
  发出时刻，不另行估算。用量只有输入、输出Token，记为partial。工具调用同上。

没有结束时间的调用不生成步骤。正文写入步骤，是否导出由运行计划的内容级别决定：工具调用为参数与输出；
公开事件路径的模型调用为本次新增的输入与回复、发出的工具调用；Harbor试次登记了状态包时，
每次上下文压缩另成一次模型调用步骤（摘要或失败原因），成功的摘要也计入其后那次调用的新增输入。
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from evalkit.contracts import ArmTrials, Usage
from evalkit.files import read_artifact, read_model, write_model
from evalkit.steps import Step, StepTrace, step_text

from harnessix.agent.models import ItemFinished, ToolCallContent, ToolResultContent, Turn
from harnessix.agent.usage import ModelAttempt
from harnessix.context.compaction_ledger_contracts import CompactionRecord
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.product_config.state_backup_validation import verification_publication_binding
from harnessix.protocol.contracts import (
    ItemPublicEvent,
    PublicEvent,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolCallRejectionContent,
    PublicToolResultContent,
    TurnStartedPublicEvent,
    UsagePublicEvent,
)
from harnessix.session.sqlite import SQLiteSessionStore
from integrations.harnessix_eval.harbor_parser import (
    EVENTS_ARTIFACT,
    STATE_ARTIFACT,
    STATE_ARTIFACT_MAX_BYTES,
)

_MAX_EVENTS_BYTES = 32 * 1024 * 1024


def _generation(attempt: ModelAttempt) -> Step | None:
    if attempt.finished_at is None:
        return None
    usage = attempt.usage
    return Step(
        step_id=str(attempt.attempt_id),
        kind="generation",
        name="model",
        started_at=attempt.started_at,
        completed_at=attempt.finished_at,
        status="ok" if attempt.status == "completed" else "error",
        model=attempt.actual_model or attempt.requested_model,
        usage=Usage(
            completeness=usage.completeness,
            model_calls=1,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cache_read_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_output_tokens,
        ),
        metadata={"step": str(attempt.step), "attempt": str(attempt.index)},
    )


def turn_steps(turn: Turn, finished: list[tuple[datetime, ItemFinished]]) -> tuple[Step, ...]:
    steps = [step for attempt in turn.model_attempts if (step := _generation(attempt))]
    calls: dict[UUID, tuple[datetime, ToolCallContent]] = {}
    for moment, event in finished:
        content = event.content
        if isinstance(content, ToolCallContent):
            calls[content.call_id] = (moment, content)
        elif isinstance(content, ToolResultContent) and content.call_id in calls:
            started, call = calls.pop(content.call_id)
            steps.append(
                Step(
                    step_id=str(content.call_id),
                    kind="tool",
                    name=call.tool,
                    started_at=started,
                    completed_at=max(moment, started),
                    status="ok" if content.outcome == "succeeded" else "error",
                    input_text=json.dumps(call.arguments, ensure_ascii=False),
                    output_text=json.dumps(content.output, ensure_ascii=False),
                    metadata={"outcome": content.outcome, "effect": str(call.effect_class)},
                )
            )
    return tuple(sorted(steps, key=lambda step: step.started_at))


async def step_trace(
    database: Path, thread_id: UUID, turn_id: UUID, *, plan_digest: str, slot: int
) -> StepTrace:
    """读取会话库副本中的一个Turn；会话库本身不被打开写入。"""
    with tempfile.TemporaryDirectory(prefix="harnessix-steps-") as temporary:
        copy = Path(temporary) / database.name
        for suffix in ("", "-wal", "-shm"):
            source = database.with_name(database.name + suffix)
            if source.exists():
                shutil.copyfile(source, copy.with_name(copy.name + suffix))
        store = SQLiteSessionStore(copy)
        thread = await store.get_thread(thread_id)
        turn = next(turn for turn in thread.turns if turn.turn_id == turn_id)
        finished = [
            (event.occurred_at, event.payload)
            for event in await store.events(thread_id)
            if event.turn_id == turn_id and isinstance(event.payload, ItemFinished)
        ]
    return StepTrace(plan_digest=plan_digest, slot=slot, steps=turn_steps(turn, finished))


@dataclass
class _Generation:
    """用量记录确定一次模型调用；它的回复文字在用量之前、发出的工具调用在用量之后陆续出现。"""

    step: int
    started_at: datetime
    completed_at: datetime
    usage: Usage
    received: list[tuple[str, str]]
    output: list[tuple[str, str]]


def _result_text(content: PublicToolResultContent) -> str:
    if content.error is None:
        return json.dumps(content.output, ensure_ascii=False)
    error = content.error.model_dump(mode="json")
    return json.dumps({"output": content.output, "error": error}, ensure_ascii=False)


def protocol_turn_steps(
    events: Sequence[PublicEvent],
    *,
    model: str | None,
    compactions: Sequence[CompactionRecord] = (),
) -> tuple[Step, ...]:
    """一个Turn的公开协议事件 → 逐步记录（窗口口径见模块说明）。

    模型调用的输入是上次调用之后新增的内容（首次为任务，其后为工具结果，压缩后另含摘要），
    输出是回复文字与发出的工具调用，格式与mini-swe-agent相同（evalkit.steps.step_text）。
    压缩记录来自试次状态包，每次压缩是一次单独计费的模型调用。
    """
    steps: list[Step] = []
    generations: list[_Generation] = []
    boundary: datetime | None = None
    calls: dict[UUID, tuple[datetime, PublicToolCallContent]] = {}
    received: list[tuple[str, str]] = []
    replied: list[tuple[str, str]] = []
    summaries = {
        record.plan.model_step: record.summary.text
        for record in compactions
        if record.summary is not None
    }
    for event in sorted(events, key=lambda item: item.cursor):
        data = event.data
        if isinstance(data, TurnStartedPublicEvent):
            boundary = event.occurred_at
        elif isinstance(data, UsagePublicEvent):
            if data.step in summaries:
                received.append(("context_compacted", summaries[data.step]))
            if boundary is not None and event.occurred_at >= boundary:
                usage = Usage(
                    completeness="partial",
                    model_calls=1,
                    input_tokens=data.usage.input_tokens,
                    output_tokens=data.usage.output_tokens,
                )
                generations.append(
                    _Generation(data.step, boundary, event.occurred_at, usage, received, replied)
                )
            received, replied = [], []
            boundary = event.occurred_at
        elif isinstance(data, ItemPublicEvent) and data.type == "item_finished":
            content = data.item.content
            if isinstance(content, PublicTextContent):
                target = received if content.kind == "user_message" else replied
                target.append((content.kind, content.text))
            elif isinstance(content, PublicToolCallContent):
                calls[content.call_id] = (event.occurred_at, content)
                if generations:
                    arguments = json.dumps(content.arguments, ensure_ascii=False)
                    generations[-1].output.append((f"tool_call {content.tool}", arguments))
            elif isinstance(content, PublicToolCallRejectionContent):
                received.append(("tool_call_rejection", content.model_dump_json()))
            elif isinstance(content, PublicToolResultContent):
                boundary = event.occurred_at
                pair = calls.pop(content.call_id, None)
                if pair is None:
                    continue
                started, call = pair
                result = _result_text(content)
                received.append((f"tool_result {call.tool}", result))
                steps.append(
                    Step(
                        step_id=str(content.call_id),
                        kind="tool",
                        name=call.tool,
                        started_at=started,
                        completed_at=max(event.occurred_at, started),
                        status="ok" if content.outcome == "succeeded" else "error",
                        input_text=json.dumps(call.arguments, ensure_ascii=False),
                        output_text=result,
                        metadata={"outcome": content.outcome, "effect": call.effect_class},
                    )
                )
    turn_id = events[0].turn_id if events else None
    steps.extend(
        Step(
            step_id=f"model-{turn_id}-{g.step}",
            kind="generation",
            name="model",
            started_at=g.started_at,
            completed_at=g.completed_at,
            status="ok",
            model=model,
            usage=g.usage,
            input_text=step_text(g.received),
            output_text=step_text(g.output),
            metadata={"step": str(g.step), "window": "protocol_events"},
        )
        for g in generations
    )
    steps.extend(step for record in compactions if (step := _compaction_step(record)))
    return tuple(sorted(steps, key=lambda step: step.started_at))


def _compaction_step(record: CompactionRecord) -> Step | None:
    """一次上下文压缩的摘要请求；失败的压缩同样计费，输出记失败原因。"""
    attempt = record.attempt
    if attempt is None or attempt.finished_at is None:
        return None
    if record.summary is not None:
        output = step_text([("summary", record.summary.text)])
    elif record.failure is not None:
        output = step_text([("failure", f"{record.failure.code}: {record.failure.message}")])
    else:
        output = None
    usage = attempt.usage
    return Step(
        step_id=f"compaction-{record.plan.compaction_id}",
        kind="generation",
        name="compaction",
        started_at=attempt.started_at,
        completed_at=attempt.finished_at,
        status="ok" if record.status == "summarized" else "error",
        model=attempt.actual_model or attempt.requested_model,
        usage=Usage(
            completeness=usage.completeness,
            model_calls=1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ),
        output_text=output,
        metadata={"step": str(record.plan.model_step), "status": record.status},
    )


def state_compactions(body: bytes, thread_id: UUID, turn_id: UUID) -> tuple[CompactionRecord, ...]:
    """从试次状态包只读取该Turn的压缩记录：解压到私有临时目录，以只验真Binding打开会话库副本。"""
    with tempfile.TemporaryDirectory(prefix="harnessix-state-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            # tar过滤器同样拒绝绝对路径与越界成员，但保留目录权限；data过滤器会丢掉目录的0700，
            # 私有状态目录随即被拒绝打开。
            archive.extractall(temporary, filter="tar")
        databases = list(Path(temporary).glob("*/sessions.db"))
        if len(databases) != 1:
            raise ValueError("试次状态包应恰好包含一个会话库")
        return asyncio.run(_turn_compactions(databases[0].parent, thread_id, turn_id))


async def _turn_compactions(
    state: Path, thread_id: UUID, turn_id: UUID
) -> tuple[CompactionRecord, ...]:
    with PrivateStateTree(state) as tree, verification_publication_binding(tree) as binding:
        thread = await SQLiteSessionStore(state / "sessions.db", publication=binding).get_thread(
            thread_id
        )
    turn = next((turn for turn in thread.turns if turn.turn_id == turn_id), None)
    if turn is None:
        raise ValueError("试次状态包中没有该Turn")
    return tuple(turn.compactions)


def parse_protocol_events(body: bytes) -> tuple[PublicEvent, ...]:
    return tuple(
        PublicEvent.model_validate_json(line) for line in body.decode("utf-8").splitlines() if line
    )


def export_protocol_steps(
    run_dir: Path, trials_path: Path, output: Path, *, model: str | None
) -> int:
    """按试次登记的事件制品（核对摘要）生成逐步记录，每个试次一个0600文件。"""
    group = read_model(trials_path, ArmTrials, 64 * 1024 * 1024, require_private=True)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    written = 0
    for trial in group.trials:
        ref = next((a for a in trial.artifacts if a.name == EVENTS_ARTIFACT), None)
        if ref is None:
            continue
        events = parse_protocol_events(read_artifact(ref, run_dir, _MAX_EVENTS_BYTES))
        state = next((a for a in trial.artifacts if a.name == STATE_ARTIFACT), None)
        refs = trial.system_refs
        compactions = (
            state_compactions(
                read_artifact(state, run_dir, STATE_ARTIFACT_MAX_BYTES),
                UUID(refs["thread_id"]),
                UUID(refs["turn_id"]),
            )
            if state is not None and "thread_id" in refs and "turn_id" in refs
            else ()
        )
        steps = protocol_turn_steps(events, model=model, compactions=compactions)
        if not steps:
            continue
        trace = StepTrace(plan_digest=group.plan_digest, slot=trial.slot, steps=steps)
        write_model(output / f"{group.arm_id}-slot{trial.slot}.json", trace, 64 * 1024 * 1024)
        written += 1
    return written
