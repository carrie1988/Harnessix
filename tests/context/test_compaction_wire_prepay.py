"""窄安全回归：真实 mapper/SDK Mock 验证完整预付，不证明性能或实际 Token 节省。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import httpx2
import pytest

from harnessix.agent import model_history_runtime
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.compaction_input_budget import (
    prepare_compaction_input_budget,
    reserved_summary_item,
)
from harnessix.agent.errors import KernelError
from harnessix.agent.model_history_runtime import _budget_plan, _prepaid_pressure_plan
from harnessix.agent.model_input_budget import estimate_model_input_tokens
from harnessix.agent.models import Usage
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS
from harnessix.agent.turn_budget_compaction import (
    closing_reserve,
    pressure_summary_instructions,
    require_request_slots,
    summary_output_limit,
    summary_request_item,
)
from harnessix.context.compaction import replay_compaction_plan, validate_compaction
from harnessix.context.compaction_contracts import CompactionSummary
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.tool_result_view import history_document
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models._history import budget_instruction, encode_json, messages_for, tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import (
    CompactionSummaryProjection,
    ModelInputTokenEstimator,
    ResponseCompleted,
)
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.context.test_compaction import assert_paired_partition, change_turn
from tests.context.test_turn_budget_compaction import (
    preflight,
    preflight_host,
    pressure_thread,
    settled_attempt,
)
from tests.models import anthropic_wire, wire


@pytest.fixture
async def wire_host(tmp_path):
    http_calls = []

    def forbid_http(request):
        http_calls.append(request)
        raise AssertionError("纯规划不得发起 HTTP")

    config = OpenAIChatConfig(model="test-model")
    async with (
        OpenAIChatProvider(
            config, api_key="offline-fixture", transport=httpx.MockTransport(forbid_http)
        ) as main,
        OpenAIChatProvider(
            config, api_key="offline-fixture", transport=httpx.MockTransport(forbid_http)
        ) as summary,
    ):
        main.estimate_input_tokens = Mock(wraps=main.estimate_input_tokens)
        summary.estimate_input_tokens = Mock(wraps=summary.estimate_input_tokens)
        host, _, _ = preflight_host(tmp_path)
        host.provider, host._summary_provider = main, summary
        yield SimpleNamespace(host=host, main=main, summary=summary)
    assert http_calls == []


async def plan(env, source):
    turn = source.turns[0]
    history = prepare_active_model_history(
        source, turn.model_steps + 1, env.host._tool_result_view_policy
    )
    return await _budget_plan(env.host, source, turn, history, CancelToken(), SUMMARY_INSTRUCTIONS)


def input_budget(env, source, context, selection):
    history = prepare_active_model_history(
        source, source.turns[0].model_steps + 1, env.host._tool_result_view_policy
    )
    result = prepare_compaction_input_budget(
        env.main, env.summary, source, source.turns[0], history, selection, context
    )
    assert result is not None
    return result


def last_requests(env):
    summary = env.summary.estimate_input_tokens.call_args_list[-1].args[0]
    original, successor = (
        call.args[0] for call in env.main.estimate_input_tokens.call_args_list[-2:]
    )
    return summary, original, successor


async def candidate(env, source, planned, context, selection):
    fixed = estimate_model_input_tokens(
        (), selection.tools, context.instructions if context else None
    )
    return await _prepaid_pressure_plan(
        env.host,
        source,
        source.turns[0],
        CancelToken(),
        planned.plan.policy,
        fixed,
        SUMMARY_INSTRUCTIONS,
        wire_budget=input_budget(env, source, context, selection),
    )


@pytest.mark.parametrize("choice", ["auto", "required", "forced"])
async def test_complete_wire_requests_prepay_exact_projection_and_selection(wire_host, choice):
    env, source = wire_host, pressure_thread()
    descriptor = (
        env.host._model_tools_for_turn(source.turns[0])
        .tools[0]
        .model_copy(
            update={
                "description": '只读"\\\n🙂约束',
                "input_schema": {
                    "type": "object",
                    "title": "removed-metadata",
                    "properties": {"path": {"type": "string", "const": '源"\\\n🙂'}},
                },
            }
        )
    )
    selection = ModelToolSelection(
        (descriptor,),
        choice="auto" if choice == "auto" else "required",
        forced_tool=descriptor.name if choice == "forced" else None,
    )
    env.host._model_tools_for_turn = lambda _: selection
    before = source.model_dump_json()
    planned, context, selected = await plan(env, source)
    assert planned is not None and context is not None and selected == selection
    summary, original, successor = last_requests(env)
    reserve = planned.plan.policy.summary_reserve_tokens
    projected = successor.history[1]
    assert projected == reserved_summary_item(planned)
    assert len(history_document(projected)) == len(history_document(projected).encode()) == reserve
    assert successor.history == (
        planned.retained_history[0],
        projected,
        *planned.retained_history[1:],
    )
    assert json.loads(projected.content.text)["trust"] == "derived_history"
    assert successor.tools == selection.tools and successor.instructions == context.instructions
    assert (
        successor.tool_choice == selection.choice and successor.forced_tool == selection.forced_tool
    )
    assert summary.history == (summary_request_item(planned),) and summary.tools == ()
    assert summary.instructions == pressure_summary_instructions(
        SUMMARY_INSTRUCTIONS, env.host._compaction, planned
    )
    assert summary.remaining_tokens == summary_output_limit(env.host._compaction, planned)
    assert summary.consumed_usage is None and summary.input_tokens_upper_bound is None
    assert original.budget == successor.budget == summary.budget == source.turns[0].budget
    assert original.consumed_usage == successor.consumed_usage == Usage()
    assert successor.remaining_calls == source.turns[0].budget.max_steps
    assert successor.input_tokens_upper_bound == source.turns[0].budget.max_tokens
    assert all(
        call.kwargs.get("prefix_items", 0) == 0
        for provider in (env.main, env.summary)
        for call in provider.estimate_input_tokens.call_args_list
    )
    costs = input_budget(env, source, context, selection).estimate_costs(
        planned, env.host._compaction, SUMMARY_INSTRUCTIONS
    )
    summary_cost, source_input, successor_input = costs
    assert summary_cost == len(
        encode_json(chat_request(summary, env.summary.config)[0]).encode()
    ) + (512 + summary.remaining_tokens)
    assert (
        source_input == len(encode_json(chat_request(original, env.main.config)[0]).encode()) + 512
    )
    assert (
        successor_input
        == len(encode_json(chat_request(successor, env.main.config)[0]).encode()) + 512
    )
    assert summary_cost <= (source_input - successor_input) * 2
    assert summary_cost + successor_input + closing_reserve(source.turns[0]) <= 40_000
    mapped, _ = chat_request(successor, env.main.config)
    assert mapped["tools"][0]["function"]["name"] == tool_alias(descriptor.name)
    assert "title" not in mapped["tools"][0]["function"]["parameters"]
    if choice == "forced":
        assert mapped["tool_choice"]["function"]["name"] == tool_alias(descriptor.name)
    assert planned.plan.policy.retain_recent_groups == 2
    assert source.turns[0].items[0].item_id in planned.plan.pinned_item_ids
    assert set(i.item_id for i in source.turns[0].items[-6:]) <= set(planned.plan.retained_item_ids)
    assert_paired_partition(planned)
    assert replay_compaction_plan(source, planned.plan) == planned
    assert source.model_dump_json() == before


def fitted_summary(planned, fragment):
    reserve = planned.plan.policy.summary_reserve_tokens
    low, high = 1, reserve
    while low < high:
        count = (low + high + 1) // 2
        summary = CompactionSummary(
            compaction_id=planned.plan.compaction_id, text="fact " + fragment * count
        )
        if len(history_document(compaction_summary_item(summary)).encode()) <= reserve:
            low = count
        else:
            high = count - 1
    return CompactionSummary(
        compaction_id=planned.plan.compaction_id, text="fact " + fragment * low
    )


@pytest.mark.parametrize("mapping", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "fragment", ['"', "\\", "\n", "\t", "\b", "\x01", "中文", "🙂", "e\u0301", "\u2028"]
)
async def test_reserved_ascii_dominates_escaped_unicode_and_assistant_merge(
    wire_host, mapping, fragment
):
    source = pressure_thread()
    planned, _, _ = await plan(wire_host, source)
    assert planned is not None
    _, _, reserved_request = last_requests(wire_host)
    summary = fitted_summary(planned, fragment)
    validated = await validate_compaction(source, planned.plan, summary, CancelToken())
    actual = reserved_request.model_copy(
        update={
            "history": validated.history,
            "consumed_usage": Usage(input_tokens=49_999),
            "input_tokens_upper_bound": 999,
            "remaining_calls": 1,
            "remaining_tokens": 1,
        }
    )
    if mapping == "openai":
        mapper, config = chat_request, wire_host.main.config
    else:
        mapper, config = anthropic_request, AnthropicConfig(model="test-model")
    reserved_body, _ = mapper(reserved_request, config)
    actual_body, _ = mapper(actual, config)
    assert len(encode_json(actual_body).encode()) <= len(encode_json(reserved_body).encode())
    merged = messages_for(actual)[1]
    assert merged["role"] == "assistant" and merged["tool_calls"]
    assert merged["content"] == actual.history[1].content.text + actual.history[2].content.text
    assert len(budget_instruction(actual)) <= len(budget_instruction(reserved_request))


@pytest.mark.parametrize("shortfall", [0, 1])
async def test_exact_fullwire_prepaid_boundary_does_not_deduct_amortization(wire_host, shortfall):
    source = pressure_thread()
    planned, context, selection = await plan(wire_host, source)
    assert planned is not None
    summary_cost, _, successor_input = input_budget(
        wire_host, source, context, selection
    ).estimate_costs(planned, wire_host.host._compaction, SUMMARY_INSTRUCTIONS)
    cost = summary_cost + successor_input + closing_reserve(source.turns[0])
    limited = change_turn(source, usage=Usage(input_tokens=50_000 - cost + shortfall))
    result = await candidate(wire_host, limited, planned, context, selection)
    assert (result is not None) == (shortfall == 0)
    assert limited.turns[0].budget == source.turns[0].budget


async def test_legacy_amortization_pass_is_not_fullwire_pass(wire_host, tmp_path):
    source = pressure_thread()
    legacy_host, _, _ = preflight_host(tmp_path)
    legacy, context = await preflight(legacy_host, source)
    assert legacy is not None
    selection = wire_host.host._model_tools_for_turn(source.turns[0])
    costs = input_budget(wire_host, source, context, selection).estimate_costs(
        legacy, wire_host.host._compaction, SUMMARY_INSTRUCTIONS
    )
    assert costs[0] > (costs[1] - costs[2]) * 2
    assert await candidate(wire_host, source, legacy, context, selection) is None
    selected, _, _ = await plan(wire_host, source)
    assert selected is not None
    assert selected.plan.policy.summary_reserve_tokens < legacy.plan.policy.summary_reserve_tokens
    assert selected.plan.pinned_item_ids == legacy.plan.pinned_item_ids
    assert selected.plan.retained_item_ids == legacy.plan.retained_item_ids


@pytest.mark.parametrize("side", ["main", "summary"])
@pytest.mark.parametrize("value", [0, -1, True, None, 1.5, "100", RuntimeError("drift")])
async def test_invalid_fullwire_estimate_strictly_stops_without_http(wire_host, side, value):
    estimator = getattr(wire_host, side).estimate_input_tokens
    if isinstance(value, Exception):
        estimator.side_effect = value
    else:
        estimator.side_effect = None
        estimator.return_value = value
    with pytest.raises(KernelError) as caught:
        await plan(wire_host, pressure_thread())
    assert caught.value.code == "invalid_provider_output"


@pytest.mark.parametrize("side", ["main", "summary"])
async def test_increased_estimate_is_recomputed_not_replaced_by_native_or_old_usage(
    wire_host, side
):
    source = pressure_thread()
    planned, _, _ = await plan(wire_host, source)
    assert planned is not None
    provider = getattr(wire_host, side)
    estimator = provider.estimate_input_tokens
    original_estimate = estimator._mock_wraps
    estimator.side_effect = lambda request, **kwargs: original_estimate(request, **kwargs) + 50_000
    assert (await plan(wire_host, source))[0] is None


async def test_estimator_disappearing_after_admission_strictly_stops(wire_host):
    original_estimate = wire_host.summary.estimate_input_tokens._mock_wraps

    def drift(request, **kwargs):
        wire_host.main.estimate_input_tokens = None
        return original_estimate(request, **kwargs)

    wire_host.summary.estimate_input_tokens.side_effect = drift
    with pytest.raises(KernelError) as caught:
        await plan(wire_host, pressure_thread())
    assert caught.value.code == "invalid_provider_output"


@pytest.mark.parametrize("side", ["main", "summary"])
async def test_invalid_full_capability_identity_is_not_legacy_fallback(
    wire_host, monkeypatch, side
):
    provider = getattr(wire_host, side)
    monkeypatch.setattr(type(provider), "input_token_identity", property(lambda _: ("broken", "")))
    with pytest.raises(KernelError) as caught:
        await plan(wire_host, pressure_thread())
    assert caught.value.code == "invalid_provider_output"


@pytest.mark.parametrize("side", ["main", "summary"])
@pytest.mark.parametrize("missing", ["attribute", "none", "estimator", "identity", "scripted"])
async def test_missing_or_partial_capability_keeps_exact_legacy_path(
    wire_host, tmp_path, monkeypatch, side, missing
):
    source = pressure_thread()
    legacy_host, legacy_main, legacy_summary = preflight_host(tmp_path)
    compaction_id = uuid4()
    monkeypatch.setattr(model_history_runtime, "new_id", lambda: compaction_id)
    expected, expected_context, expected_selection = await plan(
        SimpleNamespace(host=legacy_host), source
    )
    attribute = "provider" if side == "main" else "_summary_provider"
    forbidden = Mock(side_effect=AssertionError("legacy 不访问残缺端口"))
    if missing == "attribute":
        delattr(wire_host.host, attribute)
    else:
        replacement = {
            "none": None,
            "estimator": SimpleNamespace(
                input_token_identity=("partial", "test-model"),
                compaction_summary_projection="native-document-json-string/v1",
            ),
            "identity": SimpleNamespace(
                estimate_input_tokens=forbidden,
                compaction_summary_projection="native-document-json-string/v1",
            ),
            "scripted": legacy_main if side == "main" else legacy_summary,
        }[missing]
        setattr(wire_host.host, attribute, replacement)
    actual, context, selection = await plan(wire_host, source)
    assert actual == expected and context == expected_context and selection == expected_selection
    wire_host.main.estimate_input_tokens.assert_not_called()
    wire_host.summary.estimate_input_tokens.assert_not_called()
    forbidden.assert_not_called()
    assert legacy_main.requests == legacy_summary.requests == []


@pytest.mark.parametrize("side", ["main", "summary"])
@pytest.mark.parametrize("marker", [None, "unproved/v1"])
async def test_third_party_estimator_without_exact_projection_promise_stays_legacy(
    wire_host, tmp_path, monkeypatch, side, marker
):
    source = pressure_thread()
    legacy_host, _, _ = preflight_host(tmp_path)
    compaction_id = uuid4()
    monkeypatch.setattr(model_history_runtime, "new_id", lambda: compaction_id)
    expected = await plan(SimpleNamespace(host=legacy_host), source)
    forbidden = Mock(side_effect=AssertionError("无投影承诺的第三方继续 legacy"))
    third_party = SimpleNamespace(
        input_token_identity=("third_party", "test-model"),
        estimate_input_tokens=forbidden,
        stream=forbidden,
    )
    if marker is not None:
        third_party.compaction_summary_projection = marker
    assert isinstance(third_party, ModelInputTokenEstimator)
    setattr(wire_host.host, "provider" if side == "main" else "_summary_provider", third_party)
    assert await plan(wire_host, source) == expected
    forbidden.assert_not_called()
    wire_host.main.estimate_input_tokens.assert_not_called()
    wire_host.summary.estimate_input_tokens.assert_not_called()


@pytest.mark.parametrize("capacity", [-1, 0, 1, 3])
async def test_empty_capacity_or_zero_output_skips_before_estimation_and_tries_next(
    wire_host, monkeypatch, capacity
):
    source = pressure_thread()
    planned, context, selection = await plan(wire_host, source)
    assert planned is not None
    one = compaction_summary_item(
        CompactionSummary(compaction_id=planned.plan.compaction_id, text="x")
    )
    wrapper_bytes = len(history_document(one).encode()) - 1
    tiny_policy = planned.plan.policy.model_copy(
        update={"summary_reserve_tokens": wrapper_bytes + capacity}
    )
    tiny = replace(planned, plan=planned.plan.model_copy(update={"policy": tiny_policy}))
    projected = reserved_summary_item(tiny)
    assert (projected is None) == (capacity <= 0)
    assert summary_output_limit(wire_host.host._compaction, tiny) == 0
    wire_host.main.estimate_input_tokens.reset_mock()
    wire_host.summary.estimate_input_tokens.reset_mock()
    assert (
        input_budget(wire_host, source, context, selection).estimate_costs(
            tiny, wire_host.host._compaction, SUMMARY_INSTRUCTIONS
        )
        is None
    )
    wire_host.main.estimate_input_tokens.assert_not_called()
    wire_host.summary.estimate_input_tokens.assert_not_called()
    monkeypatch.setattr(
        model_history_runtime, "pressure_policies", lambda *_: (tiny_policy, planned.plan.policy)
    )
    result, _, _ = await plan(wire_host, source)
    assert (
        result is not None and result.plan.policy.summary_reserve_tokens > wrapper_bytes + capacity
    )
    assert wire_host.main.estimate_input_tokens.call_count == 2
    assert wire_host.summary.estimate_input_tokens.call_count == 1


@pytest.mark.parametrize("completeness", ["unknown", "partial"])
async def test_unknown_charges_stop_before_estimation_or_http(wire_host, completeness):
    source = pressure_thread()
    source = change_turn(
        source, model_attempts=(settled_attempt(completeness=completeness, outcome="failed"),)
    )
    with pytest.raises(KernelError) as caught:
        await plan(wire_host, source)
    assert caught.value.code == "budget_exceeded"
    wire_host.main.estimate_input_tokens.assert_not_called()
    wire_host.summary.estimate_input_tokens.assert_not_called()


async def test_completed_input_anchor_never_discounts_rewritten_window(wire_host):
    source = pressure_thread()
    previous = settled_attempt(completeness="complete", outcome="completed")
    source = change_turn(source, model_steps=1, model_attempts=(previous,))
    assert require_request_slots(source.turns[0]) >= 2
    planned, _, _ = await plan(wire_host, source)
    assert planned is not None
    assert all(
        call.kwargs.get("prefix_items", 0) == 0
        for provider in (wire_host.main, wire_host.summary)
        for call in provider.estimate_input_tokens.call_args_list
    )


@pytest.mark.parametrize("mapping", ["openai", "anthropic"])
async def test_actual_sdk_mock_bodies_fit_complete_summary_and_reserved_successor(
    wire_host, mapping
):
    source = pressure_thread()
    planned, _, _ = await plan(wire_host, source)
    assert planned is not None
    summary_request, _, reserved_request = last_requests(wire_host)
    summary = fitted_summary(planned, '事实"\\\n\t\x01中文🙂e\u0301')
    validated = await validate_compaction(source, planned.plan, summary, CancelToken())
    actual = reserved_request.model_copy(
        update={
            "history": validated.history,
            "consumed_usage": Usage(input_tokens=12_345, output_tokens=99),
            "input_tokens_upper_bound": 10_000,
            "remaining_calls": 2,
            "remaining_tokens": 99,
        }
    )
    sent = []

    def handle(request):
        sent.append(request.content)
        frames = provider_wire.text_frames()
        return provider_wire.response(provider_wire.WireStream(frames))

    if mapping == "openai":
        provider_wire, mapper = wire, chat_request
        config = OpenAIChatConfig(model="test-model", enable_thinking=False)
        provider = OpenAIChatProvider(
            config, api_key="offline-fixture", transport=httpx.MockTransport(handle)
        )
    else:
        provider_wire, mapper = anthropic_wire, anthropic_request
        config = AnthropicConfig(model="test-model")
        provider = AnthropicProvider(
            config, api_key="offline-fixture", transport=httpx2.MockTransport(handle)
        )
        assert not isinstance(provider, ModelInputTokenEstimator)
    assert isinstance(provider, CompactionSummaryProjection)
    summary_bound = len(encode_json(mapper(summary_request, config)[0]).encode()) + 512
    successor_bound = len(encode_json(mapper(reserved_request, config)[0]).encode()) + 512
    assert sent == []
    async with provider:
        for request in (summary_request, actual):
            events = [event async for event in provider.stream(request, CancelToken())]
            assert any(isinstance(event, ResponseCompleted) for event in events)
    assert len(sent) == 2
    assert len(sent[0]) + 512 <= summary_bound
    assert len(sent[1]) + 512 <= successor_bound
    assert b'"extra_body"' not in sent[1]
