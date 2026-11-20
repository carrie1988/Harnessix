"""模型尝试事实的合并提交（ADR 0168 D3）：只改变批次边界，持久事件及其顺序、模型请求与错误都不变。

每个场景跑两遍：一遍合并（产品行为），一遍把每个事实都单独提交（合并之前的行为），比较持久事件、
发给模型的请求与Turn结局。ID及由ID算出的摘要在两遍之间不同，按首次出现的顺序换成占位符后逐字比较。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent import attempt_fact_commits
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import EventDraft, Thread, Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.host_step_overhead_bench import model_step, narration
from tests.agent.helpers import RecordingTools

# 两遍运行的ID不同，由ID算出的摘要也不同；都按首次出现的顺序换成占位符。
_IDS = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}")
_HELD = ["ModelUsageObserved", "ModelAttemptFinished"]


def _normalized(texts: Sequence[str]) -> list[str]:
    seen: dict[str, str] = {}
    return [
        _IDS.sub(lambda match: seen.setdefault(match[0], f"<id{len(seen)}>"), text)
        for text in texts
    ]


@dataclass(frozen=True)
class _Run:
    outcome: tuple[str, str | None]
    events: list[str]
    requests: list[str]
    batches: list[list[str]]


async def _run(tmp_path: Path, script, *, merged: bool, monkeypatch) -> _Run:  # type: ignore[no-untyped-def]
    batches: list[list[str]] = []

    class RecordingStore(SQLiteSessionStore):
        async def append(
            self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
        ) -> Thread:
            batches.append([type(draft.payload).__name__ for draft in drafts])
            return await super().append(thread_id, drafts, expected_sequence=expected_sequence)

    with monkeypatch.context() as patch:
        if not merged:
            patch.setattr(attempt_fact_commits, "holds", lambda event: False)
        store = RecordingStore(tmp_path / ("merged.db" if merged else "separate.db"))
        provider = ScriptedProvider(script)
        async with AgentRuntime(store, provider, RecordingTools()) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            try:
                turn = await runtime.run_turn(thread.thread_id, "任务", request_id="d3")
                outcome = (str(turn.status), turn.error.code if turn.error else None)
            except KernelError as error:
                outcome = ("raised", error.code)
        events = await store.events(thread.thread_id)
    payloads = [str(event.turn_id is None) + event.payload.model_dump_json() for event in events]
    return _Run(
        outcome,
        _normalized(payloads),
        _normalized([request.model_dump_json() for request in provider.requests]),
        batches,
    )


def _call(step: int, call_id: str | None = None) -> ToolCallCompleted:
    return ToolCallCompleted(call_id=call_id or f"call-{step}", tool="test.read", arguments={})


def _complete_usage(attempt: UUID, response_id: str) -> ModelUsageObserved:
    return ModelUsageObserved(
        attempt_id=attempt,
        actual_model="bench",
        response_id=response_id,
        usage=UsageObservation(completeness="complete", input_tokens=2600, output_tokens=180),
    )


def _openai_order(step: int) -> list[ProviderEvent]:
    """OpenAI Chat适配器的顺序：正文在尝试结束之后才完成，工具调用随后给出。"""
    attempt, response = uuid4(), f"response-{step}"
    usage = _complete_usage(attempt, response)
    return [
        ModelAttemptStarted(
            attempt_id=attempt, step=step, index=1, provider="openai_chat", requested_model="m"
        ),
        ResponseStarted(response_id=response),
        TextStarted(content_id="text"),
        TextDelta(content_id="text", delta="先看看"),
        usage,
        ModelAttemptFinished(attempt_id=attempt, outcome="completed"),
        TextCompleted(content_id="text", text="先看看"),
        _call(step),
        ResponseCompleted(
            finish_reason="tool_calls", usage=Usage(input_tokens=2600, output_tokens=180)
        ),
    ]


def _retried(step: int) -> list[ProviderEvent]:
    """第一次尝试连接失败（未知用量），第二次尝试成功。"""
    failed = uuid4()
    succeeded = model_step(step, [_call(step)])
    retry = succeeded[0]
    assert isinstance(retry, ModelAttemptStarted)
    error = AgentFailure(code="provider_transport", message="连接中断", retryable=True)
    return [
        retry.model_copy(update={"attempt_id": failed}),
        ModelAttemptFinished(attempt_id=failed, outcome="failed", error=error),
        retry.model_copy(update={"index": 2}),
        *succeeded[1:],
    ]


def _scenarios() -> dict[str, list[list[ProviderEvent]]]:
    bench = [model_step(step, [*narration(step, "继续"), _call(step)]) for step in (1, 2)]
    final = model_step(3, narration(3, "完成"))
    invalid = model_step(1, [_call(1)])
    invalid[3] = invalid[3].model_copy(update={"attempt_id": uuid4()})  # 用量指向不存在的尝试
    duplicate = model_step(1, [_call(1), _call(1)])
    # 用量、第一个调用、尝试结束、同ID的第二个调用：报错时两个尝试事实还留在内存里。
    duplicate.insert(4, duplicate.pop(2))
    duplicate.insert(5, duplicate.pop(2))
    incomplete = model_step(1, [_call(1)])[:-1]
    return {
        "text_before_usage": [*bench, final],
        "text_after_attempt": [_openai_order(1), _openai_order(2), final],
        "retry_after_failed_attempt": [_retried(1), model_step(2, narration(2, "完成"))],
        "usage_rejected_by_reducer": [invalid],
        "error_after_held_facts": [duplicate],
        "stream_ends_without_terminal": [incomplete],
    }


@pytest.mark.parametrize("name", list(_scenarios()))
async def test_merged_commits_persist_the_same_events_in_the_same_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    script = _scenarios()[name]

    merged = await _run(tmp_path, script, merged=True, monkeypatch=monkeypatch)
    separate = await _run(tmp_path, script, merged=False, monkeypatch=monkeypatch)

    assert merged.outcome == separate.outcome
    assert merged.events == separate.events
    assert merged.requests == separate.requests
    # 被Reducer拒绝的合并批次整批回滚、再逐个重做，所以出错的场景会多一次被拒的追加。
    if merged.outcome[0] == "completed":
        assert len(merged.batches) < len(separate.batches)
    # “尝试开始”始终单独、最先提交；合并后只有成功尝试的终态事实与响应终态同批。
    assert [b for b in merged.batches if "ModelAttemptStarted" in b] == [
        ["ModelAttemptStarted"]
    ] * sum(b == ["ModelAttemptStarted"] for b in separate.batches)
    for batch in merged.batches:
        if len(batch) > 1 and set(batch) <= {*_HELD, "UsageRecorded"}:
            assert batch[:2] == _HELD or batch == _HELD[1:] + ["UsageRecorded"]


async def test_each_successful_tool_step_saves_two_commits_when_text_comes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _scenarios()["text_before_usage"]

    merged = await _run(tmp_path, script, merged=True, monkeypatch=monkeypatch)
    separate = await _run(tmp_path, script, merged=False, monkeypatch=monkeypatch)

    assert merged.outcome == ("completed", None)
    assert merged.batches.count([*_HELD, "UsageRecorded"]) == len(script)
    assert len(separate.batches) - len(merged.batches) == 2 * len(script)


async def test_text_completing_after_the_attempt_flushes_the_held_facts_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merged = await _run(
        tmp_path, _scenarios()["text_after_attempt"], merged=True, monkeypatch=monkeypatch
    )

    assert merged.outcome == ("completed", None)
    held = [index for index, batch in enumerate(merged.batches) if batch == _HELD]
    assert len(held) == 2
    for index in held:
        assert merged.batches[index + 1] == ["ItemFinished"]
        assert merged.batches[index + 2] == ["UsageRecorded"]
