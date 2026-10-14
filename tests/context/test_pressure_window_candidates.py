"""Pressure 窗口候选回归：真实纯规划/完整 wire 估算，不调用模型或修改原任务额度。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from harnessix.agent import model_history_runtime
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.model_history_runtime import _budget_plan, _prepaid_pressure_plan
from harnessix.agent.model_input_budget import estimate_model_input_tokens
from harnessix.agent.models import Usage
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS
from harnessix.agent.turn_budget_compaction import (
    closing_reserve,
    pressure_history_allocation,
    pressure_policies,
    require_request_slots,
    summary_output_limit,
)
from harnessix.context.compaction import plan_compaction, replay_compaction_plan
from harnessix.context.compaction_ledger_contracts import CompactionRecord
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.contracts import ContextFragment, ContextFragmentKind, ContextLimits
from harnessix.context.engine import ContextEngine
from harnessix.models._chat_mapping import build_request
from harnessix.models._history import encode_json
from tests.context.test_compaction import anchor, assert_paired_partition, change_turn
from tests.context.test_compaction_wire_prepay import input_budget, last_requests
from tests.context.test_compaction_wire_prepay import wire_host as wire_host
from tests.context.test_turn_budget_compaction import (
    preflight_host,
    pressure_thread,
    settled_attempt,
)
from tests.context.test_turn_budget_compaction import summary_record as summary_record


@pytest.fixture
def pure_wire(wire_host):
    estimators = (wire_host.main.estimate_input_tokens, wire_host.summary.estimate_input_tokens)
    streams = tuple(
        Mock(side_effect=AssertionError("纯候选不得进入 Provider.stream")) for _ in estimators
    )
    wire_host.main.stream, wire_host.summary.stream = streams
    yield wire_host
    for stream in streams:
        stream.assert_not_called()
    for estimator in estimators:
        assert all(call.kwargs.get("prefix_items", 0) == 0 for call in estimator.call_args_list)
    # wire_host 自身还会检查两个 forbidding HTTP transports 均未被调用。


async def pressure_inputs(env, source):
    turn = source.turns[0]
    history = prepare_active_model_history(
        source, turn.model_steps + 1, env.host._tool_result_view_policy
    )
    selection = env.host._model_tools_for_turn(turn)
    context = await env.host._prepare_budget_context(source, history, CancelToken(), selection)
    fixed = estimate_model_input_tokens((), selection.tools, context.instructions)
    policies = pressure_policies(
        turn, env.host._compaction, fixed, estimate_model_input_tokens(history.history, (), None)
    )
    return SimpleNamespace(
        env=env, source=source, context=context, selection=selection, fixed=fixed, policies=policies
    )


@pytest.fixture
async def offline_pressure(pure_wire, tmp_path):
    # 明确复现 audit.py 的独立 offline Context，而非悄悄缩短共享 fixture 的指令。
    engine = ContextEngine(
        ContextLimits(context_window_tokens=262_144, reserved_output_tokens=2048),
        (
            ContextFragment(
                kind=ContextFragmentKind.RUNTIME_INSTRUCTION,
                source="offline-test-runtime",
                content="offline",
            ),
        ),
    )
    host, _, _ = preflight_host(tmp_path, context=engine)
    host.provider, host._summary_provider = pure_wire.main, pure_wire.summary
    pure_wire.host = host
    source = pressure_thread(groups=5, remaining=32_000)
    before = source.model_dump_json()
    case = await pressure_inputs(pure_wire, source)
    assert len(case.policies) == 3
    assert pressure_history_allocation(source.turns[0], case.fixed) > 512
    yield case
    assert source.model_dump_json() == before


@pytest.fixture
def candidate_trace(monkeypatch):
    plans = []

    async def record_plan(*args, **kwargs):
        planned = await plan_compaction(*args, **kwargs)
        plans.append(planned)
        return planned

    planner = AsyncMock(side_effect=record_plan)
    prepaid = AsyncMock(wraps=_prepaid_pressure_plan)
    monkeypatch.setattr(model_history_runtime, "plan_compaction", planner)
    monkeypatch.setattr(model_history_runtime, "_prepaid_pressure_plan", prepaid)
    return SimpleNamespace(planner=planner, prepaid=prepaid, plans=plans)


async def run_pressure(case, *, source=None, token=None):
    source = source if source is not None else case.source
    before = source.model_dump_json()
    turn = source.turns[0]
    assert turn.budget.max_tokens == 50_000 and turn.budget.max_steps == 16
    history = prepare_active_model_history(
        source, turn.model_steps + 1, case.env.host._tool_result_view_policy
    )
    try:
        return await _budget_plan(
            case.env.host, source, turn, history, token or CancelToken(), SUMMARY_INSTRUCTIONS
        )
    finally:
        assert source.model_dump_json() == before


def candidate_order(trace):
    return [
        (call.args[4], call.kwargs["preferred_history_tokens"])
        for call in trace.prepaid.await_args_list
    ]


def expected_order(case, preferences=(None, 512)):
    return [(policy, preference) for preference in preferences for policy in case.policies]


def assert_partition(source, planned):
    plan = planned.plan
    items = source.turns[0].items
    covered, retained = set(plan.covered_item_ids), set(plan.retained_item_ids)
    assert covered and not covered & retained
    assert covered | retained == {item.item_id for item in items}
    assert planned.retained_history == tuple(item for item in items if item.item_id in retained)
    assert planned.retained_history[0] == items[0]
    assert items[0].item_id in plan.pinned_item_ids
    assert {item.item_id for item in items[-6:]} <= retained
    assert plan.policy.retain_recent_groups == 2
    # 除 Call/Result 配对外，组前的 assistant 也必须和工具组一起保留/覆盖。
    for offset in range(1, len(items), 3):
        group = {item.item_id for item in items[offset : offset + 3]}
        assert group <= covered or group <= retained
    assert_paired_partition(planned)
    reserved = plan.retained_history_tokens + plan.policy.summary_reserve_tokens
    assert reserved <= plan.policy.target_history_tokens
    assert plan.source_history_tokens - reserved >= plan.policy.min_savings_tokens
    assert replay_compaction_plan(source, plan) == planned


def wire_costs(case, planned, *, source=None):
    source = source if source is not None else case.source
    costs = input_budget(case.env, source, case.context, case.selection).estimate_costs(
        planned, case.env.host._compaction, SUMMARY_INSTRUCTIONS
    )
    assert costs is not None
    summary, original, successor = last_requests(case.env)
    assert summary.budget == original.budget == successor.budget == source.turns[0].budget
    # 从真实 mapper 计算，而非固定 audit 中那一组绝对字节数。
    requests = (summary, original, successor)
    providers = (case.env.summary, case.env.main, case.env.main)
    for index, (request, provider) in enumerate(zip(requests, providers, strict=True)):
        body, _ = build_request(request, provider.config)
        output = summary.remaining_tokens if index == 0 else 0
        assert costs[index] == len(encode_json(body).encode()) + 512 + output
    return costs


async def test_offline_counterexample_uses_tight_window_only_after_all_original_capacities(
    offline_pressure, candidate_trace
):
    case, trace = offline_pressure, candidate_trace
    selected, context, selection = await run_pressure(case)
    assert selected is not None and context == case.context and selection == case.selection
    assert candidate_order(trace) == expected_order(case)
    assert trace.planner.await_count == len(trace.plans) == 6
    assert selected is trace.plans[-1]
    allocation = pressure_history_allocation(case.source.turns[0], case.fixed)
    for index, call in enumerate(trace.planner.await_args_list):
        policy = case.policies[index % len(case.policies)]
        preference = min(policy.target_history_tokens, max(512, allocation)) if index < 3 else 512
        assert call.args[3] == policy
        assert call.kwargs["preferred_history_tokens"] == preference
        assert_partition(case.source, trace.plans[index])

    remaining = case.source.turns[0].budget.max_tokens - case.source.turns[0].usage.total_tokens
    close = closing_reserve(case.source.turns[0])
    for original in trace.plans[:3]:
        summary_cost, original_input, successor_input = wire_costs(case, original)
        assert summary_cost > 2 * (original_input - successor_input)
        assert summary_cost + successor_input + close <= remaining
    old = trace.plans[2]
    assert selected.plan.policy.summary_reserve_tokens == old.plan.policy.summary_reserve_tokens
    assert summary_output_limit(case.env.host._compaction, selected) == 256
    assert summary_output_limit(case.env.host._compaction, old) == 256
    assert selected.plan.policy.target_history_tokens < old.plan.policy.target_history_tokens
    assert set(old.plan.retained_item_ids) > set(selected.plan.retained_item_ids)
    assert set(selected.plan.covered_item_ids) > set(old.plan.covered_item_ids)
    assert selected.retained_history == (
        case.source.turns[0].items[0],
        *case.source.turns[0].items[-6:],
    )
    summary_cost, original_input, successor_input = wire_costs(case, selected)
    assert original_input - successor_input < summary_cost <= 2 * (original_input - successor_input)
    assert summary_cost + 2 * (successor_input + close) <= remaining


async def test_existing_usable_capacity_returns_identical_plan_without_tight_attempts(
    pure_wire, candidate_trace, monkeypatch
):
    case = await pressure_inputs(pure_wire, pressure_thread())
    identity = uuid4()
    monkeypatch.setattr(model_history_runtime, "new_id", lambda: identity)
    expected = None
    tried = []
    for policy in case.policies:
        tried.append((policy, None))
        expected = await _prepaid_pressure_plan(
            case.env.host,
            case.source,
            case.source.turns[0],
            CancelToken(),
            policy,
            case.fixed,
            SUMMARY_INSTRUCTIONS,
            wire_budget=input_budget(case.env, case.source, case.context, case.selection),
        )
        if expected is not None:
            break
    assert expected is not None
    candidate_trace.planner.reset_mock()
    candidate_trace.plans.clear()
    selected, context, selection = await run_pressure(case)
    assert selected == expected and context == case.context and selection == case.selection
    assert candidate_order(candidate_trace) == tried
    assert candidate_trace.planner.await_count == len(tried) <= 3
    assert_partition(case.source, selected)


@pytest.mark.parametrize("success_index", range(6))
async def test_dispatch_stops_at_first_usable_candidate(
    offline_pressure, candidate_trace, success_index
):
    case = offline_pressure
    usable, _, _ = await run_pressure(case)
    assert usable is not None
    candidate_trace.prepaid.reset_mock()
    candidate_trace.prepaid.side_effect = [None] * success_index + [usable]
    selected, _, _ = await run_pressure(case)
    assert selected is usable
    assert candidate_order(candidate_trace) == expected_order(case)[: success_index + 1]


@pytest.mark.parametrize("side", ["main", "summary"])
async def test_increased_wire_cost_rejects_all_six_pure_candidates(
    offline_pressure, candidate_trace, side
):
    estimator = getattr(offline_pressure.env, side).estimate_input_tokens
    original = estimator._mock_wraps
    estimator.side_effect = lambda request, **kwargs: original(request, **kwargs) + 50_000
    assert await run_pressure(offline_pressure) == (None, None, None)
    assert candidate_order(candidate_trace) == expected_order(offline_pressure)
    assert candidate_trace.planner.await_count == len(candidate_trace.plans) == 6


@pytest.mark.parametrize("allocation", [511, 512, 513])
async def test_allocation_threshold_does_not_duplicate_the_already_tight_round(
    offline_pressure, candidate_trace, monkeypatch, allocation
):
    # 隔离 allocation 边界，用 wire 费用增长拒绝每个候选；原策略、Context 和预算不变。
    monkeypatch.setattr(model_history_runtime, "pressure_history_allocation", lambda *_: allocation)
    estimator = offline_pressure.env.summary.estimate_input_tokens
    original = estimator._mock_wraps
    estimator.side_effect = lambda request, **kwargs: original(request, **kwargs) + 50_000
    assert await run_pressure(offline_pressure) == (None, None, None)
    preferences = (None, 512) if allocation > 512 else (None,)
    assert candidate_order(candidate_trace) == expected_order(offline_pressure, preferences)
    assert candidate_trace.planner.await_count == len(offline_pressure.policies) * len(preferences)
    for call, (_, preference) in zip(
        candidate_trace.planner.await_args_list,
        expected_order(offline_pressure, preferences),
        strict=True,
    ):
        assert call.kwargs["preferred_history_tokens"] == (
            max(512, allocation) if preference is None else preference
        )


@pytest.mark.parametrize("slots", [1, 2, 3, 16])
async def test_tight_candidate_keeps_min_three_request_amortization_horizon(
    offline_pressure, slots
):
    case = offline_pressure
    selected, _, _ = await run_pressure(case)
    assert selected is not None
    source = change_turn(case.source, model_steps=16 - slots)
    before = source.model_dump_json()
    assert require_request_slots(source.turns[0]) == slots
    summary_cost, original_input, successor_input = wire_costs(case, selected, source=source)
    savings = original_input - successor_input
    assert savings < summary_cost <= 2 * savings
    assert summary_cost + successor_input + closing_reserve(source.turns[0]) <= 32_000
    accepted = await _prepaid_pressure_plan(
        case.env.host,
        source,
        source.turns[0],
        CancelToken(),
        case.policies[-1],
        case.fixed,
        SUMMARY_INSTRUCTIONS,
        wire_budget=input_budget(case.env, source, case.context, case.selection),
        preferred_history_tokens=512,
    )
    assert (accepted is not None) == (slots >= 3)
    if accepted is not None:
        assert_partition(source, accepted)
    assert source.model_dump_json() == before


@pytest.mark.parametrize("shortfall", [0, 1, 1024])
async def test_tight_window_still_requires_full_prepay_without_deducting_savings(
    offline_pressure, shortfall
):
    case = offline_pressure
    selected, _, _ = await run_pressure(case)
    assert selected is not None
    summary_cost, original_input, successor_input = wire_costs(case, selected)
    assert summary_cost <= 2 * (original_input - successor_input)
    prepaid = summary_cost + successor_input + closing_reserve(case.source.turns[0])
    source = change_turn(case.source, usage=Usage(input_tokens=50_000 - prepaid + shortfall))
    before = source.model_dump_json()
    accepted = await _prepaid_pressure_plan(
        case.env.host,
        source,
        source.turns[0],
        CancelToken(),
        case.policies[-1],
        case.fixed,
        SUMMARY_INSTRUCTIONS,
        wire_budget=input_budget(case.env, source, case.context, case.selection),
        preferred_history_tokens=512,
    )
    assert (accepted is not None) == (shortfall == 0)
    assert source.turns[0].budget == case.source.turns[0].budget
    assert source.model_dump_json() == before


@pytest.mark.parametrize(
    "kind", ["main-unknown", "summary-partial", "summary-unknown", "no-intent"]
)
async def test_unknown_cost_blocks_before_any_window_candidate(
    offline_pressure, candidate_trace, summary_record, kind
):
    if kind == "main-unknown":
        source = change_turn(
            offline_pressure.source,
            model_attempts=(settled_attempt(completeness="unknown", outcome="failed"),),
        )
    else:
        updates = (
            {"attempt": None, "unaccounted_request_possible": True}
            if kind == "no-intent"
            else {
                "attempt": settled_attempt(
                    completeness=kind.split("-")[1], outcome="failed"
                ).model_copy(
                    update={
                        "started_at": summary_record.created_at,
                        "finished_at": summary_record.finished_at,
                    }
                )
            }
        )
        record = CompactionRecord.model_validate(
            summary_record.model_copy(update=updates).model_dump()
        )
        source = change_turn(offline_pressure.source, compactions=(record,))
    with pytest.raises(KernelError) as caught:
        await run_pressure(offline_pressure, source=source)
    assert caught.value.code == "budget_exceeded"
    candidate_trace.prepaid.assert_not_awaited()
    candidate_trace.planner.assert_not_awaited()
    offline_pressure.env.main.estimate_input_tokens.assert_not_called()
    offline_pressure.env.summary.estimate_input_tokens.assert_not_called()


@pytest.mark.parametrize("cancel_at", [0, 4, 6], ids=["before-plan", "first-tight", "last-tight"])
async def test_cancellation_never_advances_to_another_candidate(
    offline_pressure, candidate_trace, cancel_at
):
    token = CancelToken()
    if cancel_at == 0:
        token.cancel()

    async def cancel_candidate(*args, **kwargs):
        if candidate_trace.prepaid.await_count == cancel_at:
            token.cancel()
        return await _prepaid_pressure_plan(*args, **kwargs)

    candidate_trace.prepaid.side_effect = cancel_candidate
    with pytest.raises(TurnCancelled):
        await run_pressure(offline_pressure, token=token)
    # 无外部 Source 的 Context 可直接返回；预取消仍会在首个 planner 检查点停止。
    attempted, completed = max(1, cancel_at), max(0, cancel_at - 1)
    assert candidate_order(candidate_trace) == expected_order(offline_pressure)[:attempted]
    assert candidate_trace.planner.await_count == attempted
    assert len(candidate_trace.plans) == completed
    assert offline_pressure.env.summary.estimate_input_tokens.call_count == completed


@pytest.mark.parametrize("side", ["main", "summary"])
@pytest.mark.parametrize("value", [0, -1, True, None, 1.5, "100", RuntimeError("drift")])
async def test_invalid_estimator_on_tight_round_fails_closed_without_more_candidates(
    offline_pressure, candidate_trace, side, value
):
    async def drift(*args, **kwargs):
        if candidate_trace.prepaid.await_count == 4:
            estimator = getattr(offline_pressure.env, side).estimate_input_tokens
            if isinstance(value, Exception):
                estimator.side_effect = value
            else:
                estimator.side_effect, estimator.return_value = None, value
        return await _prepaid_pressure_plan(*args, **kwargs)

    candidate_trace.prepaid.side_effect = drift
    with pytest.raises(KernelError) as caught:
        await run_pressure(offline_pressure)
    assert caught.value.code == "invalid_provider_output"
    assert candidate_order(candidate_trace) == expected_order(offline_pressure)[:4]
    assert candidate_trace.planner.await_count == 4


@pytest.mark.parametrize("side", ["main", "summary"])
async def test_estimator_disappearing_on_tight_round_is_not_a_native_fallback(
    offline_pressure, candidate_trace, monkeypatch, side
):
    async def drift(*args, **kwargs):
        if candidate_trace.prepaid.await_count == 4:
            monkeypatch.setattr(getattr(offline_pressure.env, side), "estimate_input_tokens", None)
        return await _prepaid_pressure_plan(*args, **kwargs)

    candidate_trace.prepaid.side_effect = drift
    with pytest.raises(KernelError) as caught:
        await run_pressure(offline_pressure)
    assert caught.value.code == "invalid_provider_output"
    assert candidate_order(candidate_trace) == expected_order(offline_pressure)[:4]


async def test_tight_round_preserves_explicit_anchor_as_an_entire_old_tool_group(
    offline_pressure, candidate_trace, monkeypatch
):
    case = offline_pressure
    explicit = anchor(case.source.turns[0].items[2])
    monkeypatch.setattr(model_history_runtime, "planning_history_anchors", lambda _: (explicit,))
    assert await run_pressure(case) == (None, None, None)
    assert candidate_order(candidate_trace) == expected_order(case)
    assert len(candidate_trace.plans) == 6
    group_ids = {item.item_id for item in case.source.turns[0].items[1:4]}
    for planned in candidate_trace.plans:
        assert planned.plan.anchors == (explicit,)
        assert group_ids <= set(planned.plan.pinned_item_ids)
        assert group_ids <= set(planned.plan.retained_item_ids)
        assert_partition(case.source, planned)


async def test_no_pressure_has_no_original_or_tight_candidates(offline_pressure, candidate_trace):
    source = pressure_thread(groups=0, remaining=50_000)
    assert await run_pressure(offline_pressure, source=source) == (None, None, None)
    candidate_trace.prepaid.assert_not_awaited()
    candidate_trace.planner.assert_not_awaited()
    offline_pressure.env.main.estimate_input_tokens.assert_not_called()
    offline_pressure.env.summary.estimate_input_tokens.assert_not_called()
