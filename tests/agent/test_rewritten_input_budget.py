"""改写视图锚定上界：上一视图、当前视图与模型身份都须由持久记录证明，否则回退。"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.model_input_budget import (
    _history_digest,
    estimate_rewritten_request_input_tokens,
)
from harnessix.agent.models import ToolResultContent, Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.context.tool_result_view import history_document
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_provider_input_budget import WireProvider, run

STUB = {"omitted": "test.read output", "revision": 1}


class LargeReadTools(RecordingTools):
    async def execute(self, call, cancel):
        result = await super().execute(call, cancel)
        return result.model_copy(update={"output": {"text": "源码行\n" * 1500}})


class RewritingProvider(WireProvider):
    def estimate_rewritten_input_tokens(self, request, previous_history):
        return self.adapter.estimate_rewritten_input_tokens(request, previous_history)


def read_step(step: int, input_tokens: int):
    start = ModelAttemptStarted(
        attempt_id=new_id(),
        step=step,
        index=1,
        provider="openai_chat",
        requested_model="test-model",
    )
    return [
        start,
        ResponseStarted(response_id=f"response-{step}"),
        ToolCallCompleted(call_id=f"read-{step}", tool="test.read", arguments={}),
        ModelUsageObserved(
            attempt_id=start.attempt_id,
            actual_model="test-model",
            response_id=f"response-{step}",
            usage=UsageObservation(
                completeness="complete", input_tokens=input_tokens, output_tokens=50
            ),
        ),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        ResponseCompleted(
            finish_reason="tool_calls",
            usage=Usage(input_tokens=input_tokens, output_tokens=50),
        ),
    ]


def masked(history):
    """把第一条工具结果换成占位，模拟 P2 遮蔽旧观察；其余 Item 不变。"""
    index = next(i for i, x in enumerate(history) if isinstance(x.content, ToolResultContent))
    stub = history[index].model_copy(
        update={"content": history[index].content.model_copy(update={"output": STUB})}
    )
    return (*history[:index], stub, *history[index + 1 :])


def with_view(turn, step, history):
    """模拟改写视图的持久检查记录：只替换本步视图摘要，其余事实不变。"""
    documents = tuple(history_document(item) for item in history)
    return turn.model_copy(
        update={
            "model_history_inspections": tuple(
                inspection.model_copy(
                    update={
                        "view_history_sha256": _history_digest(documents),
                        "history_items": len(documents),
                    }
                )
                if inspection.model_step == step
                else inspection
                for inspection in turn.model_history_inspections
            )
        }
    )


async def masked_run(tmp_path, provider_type=RewritingProvider):
    provider = provider_type([read_step(1, 10), read_step(2, 20), answer()])
    turn = await run(tmp_path, provider, text="任务", tools=LargeReadTools())
    previous, sent = provider.requests[1], provider.requests[2]
    current = sent.model_copy(update={"history": masked(sent.history)})
    return provider, with_view(turn, 3, current.history), previous.history, current


async def test_proven_masked_view_adds_only_changed_wire_to_actual_anchor(tmp_path):
    provider, turn, previous, current = await masked_run(tmp_path)
    bound = estimate_rewritten_request_input_tokens(provider, current, turn, previous)
    added = provider.adapter.estimate_rewritten_input_tokens(current, previous)
    assert bound == 20 + added
    assert bound < provider.adapter.estimate_input_tokens(current)


async def test_persisted_evidence_alone_proves_anchor_after_restart(tmp_path):
    provider, turn, previous, current = await masked_run(tmp_path)
    restored = await SQLiteSessionStore(tmp_path / "session.db").get_thread(current.thread_id)
    restored_turn = with_view(restored.turns[0], 3, current.history)
    assert estimate_rewritten_request_input_tokens(
        provider, current, restored_turn, previous
    ) == estimate_rewritten_request_input_tokens(provider, current, turn, previous)


async def test_unrecorded_current_view_falls_back(tmp_path):
    provider, _, previous, current = await masked_run(tmp_path)
    unpatched = (
        await SQLiteSessionStore(tmp_path / "session.db").get_thread(current.thread_id)
    ).turns[0]
    assert estimate_rewritten_request_input_tokens(provider, current, unpatched, previous) is None


async def test_unproven_previous_view_falls_back(tmp_path):
    provider, turn, previous, current = await masked_run(tmp_path)
    assert (
        estimate_rewritten_request_input_tokens(provider, current, turn, masked(previous)) is None
    )
    assert estimate_rewritten_request_input_tokens(provider, current, turn, previous[:-1]) is None


@pytest.mark.parametrize(
    "field,value",
    [("provider", "other"), ("requested_model", "other-model"), ("actual_model", "alias")],
)
async def test_changed_model_identity_cannot_anchor(tmp_path, field, value):
    provider, turn, previous, current = await masked_run(tmp_path)
    attempts = tuple(
        attempt.model_copy(update={field: value}) if attempt.step == 2 else attempt
        for attempt in turn.model_attempts
    )
    turn = turn.model_copy(update={"model_attempts": attempts})
    assert estimate_rewritten_request_input_tokens(provider, current, turn, previous) is None


async def test_incomplete_latest_usage_cannot_anchor_older_view(tmp_path):
    provider, turn, previous, current = await masked_run(tmp_path)
    attempts = tuple(
        attempt.model_copy(
            update={"usage": attempt.usage.model_copy(update={"completeness": "partial"})}
        )
        if attempt.step == 2
        else attempt
        for attempt in turn.model_attempts
    )
    turn = turn.model_copy(update={"model_attempts": attempts})
    # 最近完整锚点退回第1步，其视图不是给定的上一视图，因此不得复用。
    assert estimate_rewritten_request_input_tokens(provider, current, turn, previous) is None


async def test_provider_without_rewritten_capability_falls_back(tmp_path):
    provider, turn, previous, current = await masked_run(tmp_path, WireProvider)
    assert estimate_rewritten_request_input_tokens(provider, current, turn, previous) is None


@pytest.mark.parametrize("value", [0, -1, None, 1.5, True, RuntimeError("failed")])
async def test_invalid_rewritten_estimate_fails_closed(tmp_path, value):
    class InvalidProvider(RewritingProvider):
        def estimate_rewritten_input_tokens(self, request, previous_history):
            if isinstance(value, Exception):
                raise value
            return value

    provider, turn, previous, current = await masked_run(tmp_path)
    invalid = InvalidProvider([])
    with pytest.raises(KernelError) as error:
        estimate_rewritten_request_input_tokens(invalid, current, turn, previous)
    assert error.value.code == "invalid_provider_output"
