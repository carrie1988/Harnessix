"""正常主请求独立预算门：不依赖 pressure summary，也不支付网络调用。"""

from __future__ import annotations

import json

import httpx
import pytest

from harnessix.agent.model_input_budget import (
    _history_digest,
    estimate_model_input_tokens,
    estimate_request_input_tokens,
)
from harnessix.agent.models import Budget, TurnStatus, Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptStarted, ModelUsageObserved
from harnessix.context.tool_result_view import history_document
from harnessix.models._history import budget_instruction
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelInputTokenEstimator
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_runtime_sampled_token_budget import accounted_read_step
from tests.contracts.provider import model_request
from tests.models.test_provider_input_budget import pure_provider
from tests.models.wire import WireStream, response, text_frames, tool_frames


@pytest.mark.parametrize("reuse_wrapper", [True, False])
async def test_real_adapter_wrapper_proof_unlocks_next_step_without_raising_budget(
    tmp_path, reuse_wrapper
):
    class HeavyTools(RecordingTools):
        def definitions(self):
            return tuple(
                tool.model_copy(update={"description": "unchanged contract " * 300})
                for tool in super().definitions()
            )

    class UncachedProvider(OpenAIChatProvider):
        def estimate_input_tokens(self, request, *, prefix_items=0):
            self._completed_input = None
            return super().estimate_input_tokens(request, prefix_items=prefix_items)

    tools = HeavyTools()
    sample = model_request().model_copy(
        update={
            "tools": tools.definitions(),
            "remaining_tokens": 900,
            "consumed_usage": Usage(),
            "input_tokens_upper_bound": 100_000,
        }
    )
    maximum = pure_provider().estimate_input_tokens(sample) + 100
    sent = []

    def handle(request):
        sent.append(request)
        frames = tool_frames() if len(sent) == 1 else text_frames()
        return response(WireStream(frames))

    adapter = OpenAIChatProvider if reuse_wrapper else UncachedProvider
    async with adapter(
        OpenAIChatConfig(model="test-model"),
        api_key="offline-fixture",
        transport=httpx.MockTransport(handle),
    ) as provider:
        turn = await run(
            tmp_path,
            provider,
            text="测试任务",
            tools=tools,
            budget=Budget(max_tokens=maximum, max_output_chars=900),
        )
    assert turn.budget.max_tokens == maximum and turn.usage.total_tokens <= maximum
    if reuse_wrapper:
        assert len(sent) == 2 and turn.status is TurnStatus.COMPLETED
    else:
        assert len(sent) == 1 and turn.error.code == "budget_exceeded"


class WireProvider(ScriptedProvider):
    def __init__(self, steps):
        super().__init__(steps)
        self.adapter = pure_provider()
        self.estimates = []

    @property
    def input_token_identity(self):
        return self.adapter.input_token_identity

    def estimate_input_tokens(self, request, *, prefix_items=0):
        self.estimates.append(request)
        return self.adapter.estimate_input_tokens(request, prefix_items=prefix_items)


async def run(tmp_path, provider, *, text="test", budget=None, tools=None):
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"), provider, tools
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        return await runtime.run_turn(thread.thread_id, text, request_id="budget", budget=budget)


async def test_runtime_feedback_uses_accounted_cumulative_usage_not_output_allowance(tmp_path):
    provider = WireProvider([accounted_read_step(input_tokens=10), answer()])
    turn = await run(tmp_path, provider, tools=RecordingTools())
    assert turn.status is TurnStatus.COMPLETED and len(provider.requests) == 2
    first, second = provider.requests
    assert first.consumed_usage == Usage()
    assert second.consumed_usage == Usage(input_tokens=10, output_tokens=50)
    assert second.remaining_tokens != second.budget.max_tokens - second.consumed_usage.total_tokens
    assert f"{second.budget.max_tokens - 60} cumulative" in budget_instruction(second)
    restored = (
        await SQLiteSessionStore(tmp_path / "session.db").get_thread(first.thread_id)
    ).turns[0]
    assert restored.model_attempts[0].usage.input_tokens == second.consumed_usage.input_tokens


@pytest.mark.parametrize("value", [True, False, 0, -1, None, 1.5, "100", RuntimeError("failed")])
async def test_invalid_capability_failclosed_zero_request(tmp_path, value):
    class InvalidProvider(WireProvider):
        def estimate_input_tokens(self, request, *, prefix_items=0):
            if isinstance(value, Exception):
                raise value
            return value

    provider = InvalidProvider([answer()])
    turn = await run(tmp_path, provider)
    assert turn.error.code == "invalid_provider_output"
    assert turn.model_steps == 0 and turn.usage == Usage()
    assert provider.requests == []


async def test_full_wire_over_budget_zero_request(tmp_path):
    provider = WireProvider([answer()])
    turn = await run(
        tmp_path, provider, text="x" * 500, budget=Budget(max_tokens=800, max_output_chars=900)
    )
    assert turn.error.code == "budget_exceeded"
    assert turn.model_steps == 0 and provider.requests == []


async def test_large_unsent_title_admitted_candidate_before_step_transition(tmp_path):
    class TitleTools(RecordingTools):
        def definitions(self):
            return tuple(
                tool.model_copy(
                    update={
                        "input_schema": {
                            "type": "object",
                            "title": "not-sent" * 10_000,
                        }
                    }
                )
                for tool in super().definitions()
            )

    provider = WireProvider([answer()])
    turn = await run(
        tmp_path,
        provider,
        tools=TitleTools(),
        budget=Budget(max_tokens=2_000, max_output_chars=900),
    )
    assert turn.status is TurnStatus.COMPLETED
    candidate, request = provider.estimates[0], provider.requests[0]
    assert candidate.step == request.step == 1
    assert candidate.input_tokens_upper_bound == 2_000
    assert request.input_tokens_upper_bound == provider.adapter.estimate_input_tokens(candidate)
    assert candidate.remaining_tokens == 900
    assert provider.adapter.estimate_input_tokens(request) + request.remaining_tokens <= 2_000


async def test_third_party_fallback_unchanged(tmp_path):
    provider = ScriptedProvider([answer()])
    turn = await run(tmp_path, provider, budget=Budget(max_tokens=2_000, max_output_chars=900))
    request = provider.requests[0]
    legacy = estimate_model_input_tokens(request.history, request.tools, request.instructions)
    assert request.consumed_usage is None
    assert request.input_tokens_upper_bound is None
    assert estimate_request_input_tokens(provider, request, turn, legacy) == legacy
    assert not isinstance(provider, ModelInputTokenEstimator)
    assert not isinstance(object.__new__(AnthropicProvider), ModelInputTokenEstimator)
    assert request.remaining_tokens == min(900, 2_000 - legacy)


async def test_real_sdk_bounded_source_pages_have_prepaid_feedback_and_durable_evidence(tmp_path):
    from harnessix.agent.models import ToolResultContent
    from harnessix.tools.runtime import CodingToolRuntime
    from tests.models.wire import call as wire_call
    from tests.models.wire import chunk, frame

    body = ("print('focused source evidence')\n" * 200).encode()
    (tmp_path / "source.py").write_bytes(body)
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert any(
            "conservative input bound" in m.get("content", "")
            for m in payload["messages"]
            if m["role"] == "system"
        )
        if len(sent) == 3:
            return response(WireStream(text_frames()))
        arguments = {"path": "source.py", "max_lines": 2000, "max_utf8_bytes": 4096}
        if len(sent) == 2:
            observed = json.loads(
                next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
            )["output"]
            assert observed["utf8_bytes"] <= 4096 and observed["truncated"]
            arguments.update(
                start_line=observed["next_line"], expected_revision=observed["revision"]
            )
        tool = wire_call(arguments=json.dumps(arguments))
        tool["function"]["name"] = "read_file"
        frames = [
            frame(chunk({"tool_calls": [tool]})),
            frame(chunk(finish="tool_calls")),
            frame(chunk(usage=True)),
            b"data: [DONE]\n\n",
        ]
        return response(WireStream(frames))

    async with (
        CodingToolRuntime(tmp_path) as tools,
        OpenAIChatProvider(
            OpenAIChatConfig(model="test-model"),
            api_key="offline-fixture",
            transport=httpx.MockTransport(handle),
        ) as provider,
    ):
        turn = await run(
            tmp_path, provider, tools=tools, budget=Budget(max_tokens=50_000, max_steps=16)
        )
    assert turn.status == TurnStatus.COMPLETED and len(sent) == 3
    outputs = [
        item.content.output for item in turn.items if isinstance(item.content, ToolResultContent)
    ]
    assert len(outputs) == 2 and all(o["utf8_bytes"] <= 4096 for o in outputs)
    assert (outputs[0]["text"] + outputs[1]["text"]).encode() == body
    store = SQLiteSessionStore(tmp_path / "session.db")
    thread_ids = await store.thread_ids()
    assert len(thread_ids) == 1
    assert (await store.get_thread(thread_ids[0])).turns[0] == turn
    assert (tmp_path / "source.py").read_bytes() == body


async def test_rewritten_history_does_not_use_old_actual_anchor(tmp_path):
    provider = WireProvider([wire_read_step(input_tokens=10), answer()])
    turn = await run(tmp_path, provider, tools=RecordingTools())
    request = provider.requests[1]
    first = request.history[0]
    rewritten = (
        first.model_copy(
            update={"content": first.content.model_copy(update={"text": '重写"\\' * 1000})}
        ),
        *request.history[1:],
    )
    request = request.model_copy(update={"history": rewritten})
    digest = _history_digest(tuple(history_document(item) for item in rewritten))
    inspections = tuple(
        inspection.model_copy(update={"view_history_sha256": digest})
        if inspection.model_step == 2
        else inspection
        for inspection in turn.model_history_inspections
    )
    turn = turn.model_copy(update={"model_history_inspections": inspections})
    legacy = estimate_model_input_tokens(rewritten, request.tools, None, turn=turn, model_step=2)
    full = estimate_model_input_tokens(rewritten, request.tools, None)
    assert legacy == full and legacy > 10
    expected = provider.adapter.estimate_input_tokens(request)
    assert estimate_request_input_tokens(provider, request, turn, legacy) == expected


async def test_quote_result_cannot_turn_descriptor_underestimate_into_wire_bound(tmp_path):
    class QuoteTools(RecordingTools):
        async def execute(self, call, cancel):
            result = await super().execute(call, cancel)
            return result.model_copy(update={"output": '"\\' * 4000})

    provider = WireProvider([wire_read_step(input_tokens=10), answer()])
    turn = await run(tmp_path, provider, tools=QuoteTools())
    request = provider.requests[1]
    legacy = estimate_model_input_tokens(
        request.history, request.tools, None, turn=turn, model_step=2
    )
    wire = provider.adapter.estimate_input_tokens(request)
    assert wire > legacy
    prefix = turn.model_history_inspections[0].history_items
    incremental = provider.adapter.estimate_input_tokens(request, prefix_items=prefix)
    assert estimate_request_input_tokens(provider, request, turn, legacy) == min(
        wire, 10 + incremental
    )
    assert incremental > legacy


async def test_provider_actual_usage_still_hard_stops_tool_dispatch(tmp_path):
    tools = RecordingTools()
    provider = WireProvider([accounted_read_step(input_tokens=50_001), answer()])
    turn = await run(
        tmp_path,
        provider,
        tools=tools,
        budget=Budget(max_tokens=50_000, max_steps=16, max_output_chars=900),
    )
    assert turn.error.code == "budget_exceeded"
    assert turn.usage.input_tokens == 50_001 and turn.usage.output_tokens == 50
    assert len(provider.requests) == 1 and tools.calls == []


def wire_read_step(*, input_tokens):
    events = accounted_read_step(input_tokens=input_tokens)
    return [
        event.model_copy(update={"provider": "openai_chat", "requested_model": "test-model"})
        if isinstance(event, ModelAttemptStarted)
        else event.model_copy(update={"actual_model": "test-model"})
        if isinstance(event, ModelUsageObserved)
        else event
        for event in events
    ]


async def test_proven_anchor_uses_only_added_wire_and_full_current_catalog(tmp_path):
    provider = WireProvider([wire_read_step(input_tokens=10), answer()])
    turn = await run(tmp_path, provider, text="initial text " * 500, tools=RecordingTools())
    request = provider.requests[1]
    previous = turn.model_history_inspections[0]
    full = provider.adapter.estimate_input_tokens(request)
    added = provider.adapter.estimate_input_tokens(request, prefix_items=previous.history_items)
    legacy = estimate_model_input_tokens(
        request.history, request.tools, None, turn=turn, model_step=2
    )
    assert full > 10 + added
    assert estimate_request_input_tokens(provider, request, turn, legacy) == 10 + added
    assert added >= 512


@pytest.mark.parametrize(
    "field,value",
    [("provider", "other"), ("requested_model", "other-model"), ("actual_model", "alias-version")],
)
async def test_changed_model_or_provider_cannot_reuse_actual_anchor(tmp_path, field, value):
    provider = WireProvider([wire_read_step(input_tokens=10), answer()])
    turn = await run(tmp_path, provider, tools=RecordingTools())
    request = provider.requests[1]
    turn = turn.model_copy(
        update={
            "model_attempts": (
                turn.model_attempts[0].model_copy(update={field: value}),
                *turn.model_attempts[1:],
            )
        }
    )
    assert estimate_request_input_tokens(
        provider, request, turn, 1
    ) == provider.adapter.estimate_input_tokens(request)


@pytest.mark.parametrize(
    "identity", [None, ("", "model"), ["openai_chat", "test-model"], (True, "model")]
)
async def test_invalid_identity_failclosed_zero_request(tmp_path, identity):
    class InvalidIdentityProvider(WireProvider):
        @property
        def input_token_identity(self):
            return identity

    provider = InvalidIdentityProvider([answer()])
    turn = await run(tmp_path, provider)
    assert turn.error.code == "invalid_provider_output"
    assert not provider.requests
