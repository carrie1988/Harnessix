"""Harnessix会话库 → evalkit逐步记录。

模型调用来自Turn的ModelAttempt（真实起止时间、实际模型、逐次用量）；工具调用来自事件流中
tool_call与tool_result两次ItemFinished的发生时间。没有结束时间的调用不生成步骤，不估算。
工具参数与输出写入步骤正文，是否导出由运行计划的内容级别决定。原库先复制再读取，证据不被改动。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

from evalkit.contracts import Usage
from evalkit.steps import Step, StepTrace

from harnessix.agent.models import ItemFinished, ToolCallContent, ToolResultContent, Turn
from harnessix.agent.usage import ModelAttempt
from harnessix.session.sqlite import SQLiteSessionStore


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
