from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.model_history_runtime import _budget_plan, _prepaid_pressure_plan
from harnessix.agent.model_input_budget import estimate_model_input_tokens
from harnessix.agent.models import Budget, Thread, TurnStatus, Usage
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS, AgentRuntime
from harnessix.agent.turn_budget_compaction import (
    closing_reserve,
    pressure_policies,
    pressure_policy,
    pressure_summary_instructions,
    require_context_unchanged,
    require_request_slots,
    summary_output_limit,
    summary_request_item,
)
from harnessix.agent.usage import ModelAttempt, UsageObservation
from harnessix.context.compaction import (
    plan_compaction,
    replay_compaction_plan,
    validate_compaction,
)
from harnessix.context.compaction_contracts import CompactionSummary
from harnessix.context.compaction_ledger_contracts import CompactionRecord
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.contracts import (
    ContextFragment,
    ContextFragmentKind,
    ContextLimits,
    PreparedContext,
)
from harnessix.context.engine import ContextEngine
from harnessix.context.sources import EnvironmentContextSource, ProjectInstructionSource
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import history_document
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start
from tests.agent.helpers import RecordingTools
from tests.context.test_compaction import (
    anchor,
    assert_paired_partition,
    change_turn,
    policy,
    text,
    thread_with,
    tool_group,
)
from tests.context.test_sources import multi_source_engine, request


def budget_config(**policy_fields) -> CompactionRuntimeConfig:
    return CompactionRuntimeConfig(
        policy=policy(
            **{
                "target_history_tokens": 65_536,
                "summary_reserve_tokens": 12_288,
                "retain_recent_groups": 2,
                **policy_fields,
            }
        ),
        trigger_history_tokens=131_072,
        max_summary_output_tokens=2048,
    )


def pressure_thread(*, groups: int = 5, remaining: int = 40_000) -> Thread:
    source = thread_with(
        (
            text("修复解析器，保留public接口。", "user_message"),
            *(item for _ in range(groups) for item in tool_group(count=1, output="fact " * 256)),
        )
    )
    return change_turn(
        source,
        budget=Budget(max_tokens=50_000, max_steps=16, timeout_seconds=900, max_output_chars=2048),
        usage=Usage(input_tokens=50_000 - remaining),
    )


async def pressure_plan(source: Thread, config: CompactionRuntimeConfig | None = None):
    config = config or budget_config()
    turn = source.turns[0]
    history = prepare_active_model_history(source, turn.model_steps + 1, ToolResultViewPolicy())
    history_input = estimate_model_input_tokens(history.history, (), None)
    selected = pressure_policy(turn, config, 12_000, history_input)
    assert selected is not None
    return await plan_compaction(
        source,
        turn.model_steps + 1,
        ToolResultViewPolicy(),
        selected,
        CancelToken(),
        compaction_id=uuid4(),
    )


def preflight_host(tmp_path: Path, *, config=None, tools=None, context=None, tool_view=None):
    main, summary = ScriptedProvider([]), ScriptedProvider([])
    engine = context or ContextEngine(
        ContextLimits(context_window_tokens=262_144, reserved_output_tokens=2048),
        (
            ContextFragment(
                kind=ContextFragmentKind.RUNTIME_INSTRUCTION,
                source="offline-test-runtime",
                content="bound " * 1400,
            ),
        ),
    )
    host = AgentRuntime(
        SQLiteSessionStore(tmp_path / "unused-preflight.sqlite"),
        main,
        tools or RecordingTools(),
        context=engine,
        compaction=config or budget_config(),
        summary_provider=summary,
        model_tool_view=tool_view,
    )
    return host, main, summary


async def preflight(host: AgentRuntime, source: Thread, *, token=None):
    turn = source.turns[0]
    history = prepare_active_model_history(source, turn.model_steps + 1, ToolResultViewPolicy())
    planned, context, _ = await _budget_plan(
        host, source, turn, history, token or CancelToken(), SUMMARY_INSTRUCTIONS
    )
    return planned, context


async def test_pressure_trigger_does_not_force_three_request_allocation_as_target():
    source = pressure_thread()
    prepared = await pressure_plan(source)
    turn, selected = source.turns[0], prepared.plan.policy
    allocation = (turn.budget.max_tokens - turn.usage.total_tokens) // 3 - 12_000
    allocation -= closing_reserve(turn)

    assert selected.target_history_tokens == prepared.plan.source_history_tokens - 256
    assert selected.target_history_tokens > allocation
    assert selected.summary_reserve_tokens == min(12288, selected.target_history_tokens // 4)
    assert selected.retain_recent_groups == 2
    assert prepared.plan.covered_item_ids
    assert prepared.plan.source_history_tokens < budget_config().trigger_history_tokens
    assert (
        prepared.plan.source_history_tokens
        - (prepared.plan.retained_history_tokens + selected.summary_reserve_tokens)
        >= selected.min_savings_tokens
    )
    assert source.turns[0].items[0].item_id in prepared.plan.pinned_item_ids
    assert {item.item_id for item in source.turns[0].items[-6:]} <= set(
        prepared.plan.retained_item_ids
    )
    assert_paired_partition(prepared)


def test_no_pressure_keeps_original_window_trigger():
    turn = pressure_thread(remaining=50_000).turns[0]
    assert pressure_policy(turn, budget_config(), 100, 1000) is None
    assert pressure_policies(turn, budget_config(), 100, 1000) == ()


@pytest.mark.parametrize("maximum", [128, 256, 300, 512, 2048])
def test_capacity_candidates_are_bounded_unique_and_preserve_original_retention(maximum):
    source = pressure_thread()
    config = budget_config().model_copy(update={"max_summary_output_tokens": maximum})
    turn = source.turns[0]
    initial = pressure_policy(turn, config, 12_000, 12_000)
    candidates = pressure_policies(turn, config, 12_000, 12_000)
    assert initial is not None and candidates[0] == initial
    reserves = [candidate.summary_reserve_tokens for candidate in candidates]
    assert reserves == sorted(set(reserves), reverse=True)
    assert 1 <= len(candidates) <= 3
    if maximum < 256:
        assert candidates == (initial,)
    wrapper = (
        len(
            history_document(
                compaction_summary_item(CompactionSummary(compaction_id=uuid4(), text="x"))
            ).encode()
        )
        - 1
    )
    for candidate in candidates:
        assert candidate.model_dump(exclude={"summary_reserve_tokens"}) == initial.model_dump(
            exclude={"summary_reserve_tokens"}
        )
    for candidate in candidates[1:]:
        capacity = (candidate.summary_reserve_tokens - wrapper) // 4
        assert capacity in {min(maximum, 512), min(maximum, 256)}


def test_small_original_reserve_does_not_gain_an_unrequested_capacity():
    turn = pressure_thread().turns[0]
    config = budget_config(summary_reserve_tokens=256)
    initial = pressure_policy(turn, config, 12_000, 12_000)
    assert pressure_policies(turn, config, 12_000, 12_000) == (initial,)


async def test_unchanged_history_does_not_need_fabricated_summary(tmp_path: Path):
    source = pressure_thread()
    original = source.model_dump_json()
    host, main, summary = preflight_host(tmp_path)
    planned, _ = await preflight(host, source)
    assert planned is not None
    prepared = prepare_active_model_history(source, 1, ToolResultViewPolicy())
    context = await host._prepare_budget_context(
        source, prepared, CancelToken(), host._model_tools_for_turn(source.turns[0])
    )
    assert context is not None
    fixed = estimate_model_input_tokens((), tuple(host._definitions.values()), context.instructions)
    assert (
        planned.plan.policy.summary_reserve_tokens
        == pressure_policies(
            source.turns[0],
            host._compaction,
            fixed,
            estimate_model_input_tokens(prepared.history, (), None),
        )[0].summary_reserve_tokens
    )
    assert source.model_dump_json() == original
    assert not main.requests and not summary.requests


async def test_pressure_prepays_only_the_same_visible_catalog_as_the_main_request(tmp_path: Path):
    class ExtraRegisteredTools(RecordingTools):
        def definitions(self):
            primary = super().definitions()[0]
            hidden = tuple(
                primary.model_copy(
                    update={
                        "name": f"unused-{index}",
                        "input_schema": {
                            "type": "object",
                            "properties": {"large": {"const": "x" * 30000}},
                        },
                    }
                )
                for index in range(2)
            )
            return primary, *hidden

    class CurrentPhaseView:
        def select(self, turn, definitions):
            return ModelToolSelection(tuple(d for d in definitions if d.name == "test.read"))

    host, main, summary = preflight_host(
        tmp_path, tools=ExtraRegisteredTools(), tool_view=CurrentPhaseView()
    )
    source = pressure_thread()
    history = prepare_active_model_history(source, 1, ToolResultViewPolicy())
    planned, context, selection = await _budget_plan(
        host, source, source.turns[0], history, CancelToken(), SUMMARY_INSTRUCTIONS
    )
    assert planned is not None and context is not None and selection is not None
    assert [d.name for d in selection.tools] == ["test.read"]
    assert selection == host._model_tools_for_turn(source.turns[0])
    assert context.inspection.tool_tokens == estimate_model_input_tokens((), selection.tools, None)
    assert (
        estimate_model_input_tokens((), tuple(host._definitions.values()), context.instructions)
        > source.turns[0].budget.max_tokens
    )
    assert set(host._definitions) == {"test.read", "unused-0", "unused-1"}
    assert not main.requests and not summary.requests


@pytest.mark.parametrize("choice", [True, None, "invalid"])
async def test_invalid_catalog_selection_is_rejected_before_summary_payment(tmp_path: Path, choice):
    class InvalidChoiceView:
        def select(self, turn, definitions):
            return ModelToolSelection(definitions, choice)

    host, main, summary = preflight_host(tmp_path, tool_view=InvalidChoiceView())
    with pytest.raises(KernelError) as caught:
        await preflight(host, pressure_thread())
    assert caught.value.code == "model_tool_view_invalid"
    assert not main.requests and not summary.requests


async def test_pressure_target_stops_at_pins_and_two_complete_recent_groups():
    source = pressure_thread(groups=12)
    before = source.model_dump_json()
    ordinary = await pressure_plan(source)
    compact = await plan_compaction(
        source,
        source.turns[0].model_steps + 1,
        ToolResultViewPolicy(),
        ordinary.plan.policy,
        CancelToken(),
        compaction_id=ordinary.plan.compaction_id,
        preferred_history_tokens=512,
    )
    required = (source.turns[0].items[0], *source.turns[0].items[-6:])
    floor = sum(len(history_document(item).encode()) for item in required)
    assert compact.retained_history == required
    assert (
        compact.plan.policy.target_history_tokens
        == floor + compact.plan.policy.summary_reserve_tokens
    )
    assert len(compact.plan.covered_item_ids) > len(ordinary.plan.covered_item_ids)
    assert compact.plan.policy.target_history_tokens < ordinary.plan.policy.target_history_tokens
    assert replay_compaction_plan(source, compact.plan) == compact
    candidate = await validate_compaction(
        source,
        compact.plan,
        CompactionSummary(
            compaction_id=compact.plan.compaction_id, text="调查事实；尚未修改或测试。"
        ),
        CancelToken(),
    )
    assert candidate.history_tokens <= compact.plan.policy.target_history_tokens
    assert source.model_dump_json() == before
    assert_paired_partition(compact)


async def test_preferred_target_at_ceiling_preserves_existing_plan_bytes():
    source = pressure_thread()
    ordinary = await pressure_plan(source)
    compact = await plan_compaction(
        source,
        source.turns[0].model_steps + 1,
        ToolResultViewPolicy(),
        ordinary.plan.policy,
        CancelToken(),
        compaction_id=ordinary.plan.compaction_id,
        preferred_history_tokens=ordinary.plan.policy.target_history_tokens,
    )
    assert compact == ordinary
    assert compact.plan.model_dump_json() == ordinary.plan.model_dump_json()


@pytest.mark.parametrize("target", [True, False, 0, 511, -1, 100_000, 512.0])
async def test_invalid_preferred_target_is_not_silently_clamped(target):
    source = pressure_thread()
    ordinary = await pressure_plan(source)
    with pytest.raises(KernelError) as caught:
        await plan_compaction(
            source,
            source.turns[0].model_steps + 1,
            ToolResultViewPolicy(),
            ordinary.plan.policy,
            CancelToken(),
            compaction_id=uuid4(),
            preferred_history_tokens=target,
        )
    assert caught.value.code == "context_compaction_invalid_policy"


async def test_tight_target_preserves_entire_explicit_anchor_group():
    source = pressure_thread(groups=12)
    ordinary = await pressure_plan(source)
    compact = await plan_compaction(
        source,
        source.turns[0].model_steps + 1,
        ToolResultViewPolicy(),
        ordinary.plan.policy,
        CancelToken(),
        compaction_id=uuid4(),
        anchors=(anchor(source.turns[0].items[5]),),
        preferred_history_tokens=512,
    )
    assert {item.item_id for item in source.turns[0].items[4:7]} <= set(
        compact.plan.pinned_item_ids
    )
    assert_paired_partition(compact)
    assert replay_compaction_plan(source, compact.plan) == compact


async def test_pressure_summary_prompt_uses_actual_cap_not_low_trust_source():
    config = budget_config()
    prepared = await pressure_plan(pressure_thread(), config)
    instructions = pressure_summary_instructions(SUMMARY_INSTRUCTIONS, config, prepared)
    assert instructions.startswith(SUMMARY_INSTRUCTIONS)
    assert f"at most {summary_output_limit(config, prepared)} tokens" in instructions
    assert "Finish normally well before" in instructions
    assert "uncertain effects" in instructions
    assert prepared.summary_source not in instructions


@pytest.mark.parametrize("remaining", [0, 1, 2000])
async def test_no_affordable_budget_never_enters_provider(tmp_path: Path, remaining: int):
    host, main, summary = preflight_host(tmp_path)
    try:
        planned, _ = await preflight(host, pressure_thread(remaining=remaining))
    except KernelError as error:
        assert remaining == 0 and error.code == "budget_exceeded"
    else:
        assert planned is None
    assert not main.requests and not summary.requests


@pytest.mark.parametrize("groups", [0, 1, 2])
async def test_pins_and_two_recent_closed_groups_leave_no_compressible_prefix(
    tmp_path: Path, groups: int
):
    host, main, summary = preflight_host(tmp_path)
    planned, _ = await preflight(host, pressure_thread(groups=groups))
    assert planned is None
    assert not main.requests and not summary.requests


async def test_retained_overflow_is_not_fixed_by_dropping_a_recent_group(tmp_path: Path):
    config = budget_config(target_history_tokens=2000, summary_reserve_tokens=1024)
    source = pressure_thread()
    with pytest.raises(KernelError) as caught:
        await pressure_plan(source, config)
    assert caught.value.code == "context_compaction_retained_overflow"
    host, main, summary = preflight_host(tmp_path, config=config)
    planned, _ = await preflight(host, source)
    assert planned is None
    assert not main.requests and not summary.requests


async def test_summary_request_estimate_includes_outer_item_json_and_instructions():
    prepared = await pressure_plan(pressure_thread())
    request_item = summary_request_item(prepared)
    estimate = estimate_model_input_tokens((request_item,), (), SUMMARY_INSTRUCTIONS)
    assert estimate == len(history_document(request_item).encode()) + len(
        SUMMARY_INSTRUCTIONS.encode()
    )
    assert estimate > prepared.plan.summary_input_tokens + len(SUMMARY_INSTRUCTIONS.encode())


async def test_summary_output_limit_leaves_room_for_full_summary_item_json():
    config = budget_config()
    prepared = await pressure_plan(pressure_thread(), config)
    projected = compaction_summary_item(
        CompactionSummary(compaction_id=prepared.plan.compaction_id, text="x")
    )
    wrapper_bytes = len(history_document(projected).encode()) - 1
    actual = summary_output_limit(config, prepared)
    assert actual == min(
        config.max_summary_output_tokens,
        max(0, (prepared.plan.policy.summary_reserve_tokens - wrapper_bytes) // 4),
    )
    assert actual < prepared.plan.policy.summary_reserve_tokens // 4


async def test_candidate_next_input_counts_full_summary_projection_not_only_body():
    source = pressure_thread()
    prepared = await pressure_plan(source)
    summary = CompactionSummary(compaction_id=prepared.plan.compaction_id, text="已定位；待验证。")
    candidate = await validate_compaction(source, prepared.plan, summary, CancelToken())
    projected_bytes = len(history_document(compaction_summary_item(summary)).encode())
    assert projected_bytes > len(summary.text.encode())
    assert candidate.history_tokens == prepared.plan.retained_history_tokens + projected_bytes
    assert candidate.history_tokens <= (
        prepared.plan.retained_history_tokens + prepared.plan.policy.summary_reserve_tokens
    )
    assert_paired_partition(prepared)


async def test_body_fitting_reserve_cannot_bypass_summary_projection_overflow():
    source = pressure_thread()
    prepared = await pressure_plan(source)
    summary = CompactionSummary(
        compaction_id=prepared.plan.compaction_id,
        text="x" * prepared.plan.policy.summary_reserve_tokens,
    )
    with pytest.raises(KernelError) as caught:
        await validate_compaction(source, prepared.plan, summary, CancelToken())
    assert caught.value.code == "context_compaction_summary_overflow"


async def test_reserve_below_minimum_summary_projection_cannot_authorize_payment(tmp_path: Path):
    config = budget_config(summary_reserve_tokens=256)
    source = pressure_thread()
    projected = compaction_summary_item(CompactionSummary(compaction_id=uuid4(), text="x"))
    assert len(history_document(projected).encode()) > config.policy.summary_reserve_tokens
    unpayable = await pressure_plan(source, config)
    assert summary_output_limit(config, unpayable) == 0
    host, main, summary = preflight_host(tmp_path, config=config)
    planned, _ = await preflight(host, source)
    assert planned is None, "合法reserve连最短摘要完整Item都放不下，预付规划必须拒绝"
    assert not main.requests and not summary.requests


@pytest.mark.parametrize("shortfall", [0, 1, 1024])
async def test_prepaid_cost_reserves_summary_next_catalog_context_and_close(
    tmp_path: Path, shortfall: int
):
    source = pressure_thread()
    host, main, summary = preflight_host(tmp_path)
    prepared, context = await preflight(host, source)
    assert prepared is not None and context is not None
    fixed = estimate_model_input_tokens((), tuple(host._definitions.values()), context.instructions)
    summary_input = estimate_model_input_tokens(
        (summary_request_item(prepared),),
        (),
        pressure_summary_instructions(SUMMARY_INSTRUCTIONS, host._compaction, prepared),
    )
    cost = summary_input + summary_output_limit(host._compaction, prepared)
    cost += prepared.plan.retained_history_tokens + prepared.plan.policy.summary_reserve_tokens
    cost += fixed + closing_reserve(source.turns[0])
    assert 0 < cost < 40_000

    # 验证同一容量的精确预付边界；较小容量的合法候选另行验收，不把它当越界。
    limited = pressure_thread(remaining=cost - shortfall)
    planned = await _prepaid_pressure_plan(
        host,
        limited,
        limited.turns[0],
        CancelToken(),
        prepared.plan.policy,
        fixed,
        SUMMARY_INSTRUCTIONS,
    )
    assert (planned is not None) == (shortfall == 0)
    assert not main.requests and not summary.requests


async def test_lower_capacity_is_selected_only_if_original_cannot_be_fully_prepaid(tmp_path: Path):
    host, main, summary = preflight_host(tmp_path)
    source = pressure_thread()
    original, context = await preflight(host, source)
    assert original is not None and context is not None
    fixed = estimate_model_input_tokens((), tuple(host._definitions.values()), context.instructions)

    def complete_cost(planned):
        return (
            estimate_model_input_tokens(
                (summary_request_item(planned),),
                (),
                pressure_summary_instructions(SUMMARY_INSTRUCTIONS, host._compaction, planned),
            )
            + summary_output_limit(host._compaction, planned)
            + planned.plan.retained_history_tokens
            + planned.plan.policy.summary_reserve_tokens
            + fixed
            + closing_reserve(source.turns[0])
        )

    remaining = complete_cost(original) - 1
    limited = pressure_thread(remaining=remaining)
    before = limited.model_dump_json()
    selected, _ = await preflight(host, limited)
    assert selected is not None
    assert selected.plan.policy.summary_reserve_tokens < original.plan.policy.summary_reserve_tokens
    assert summary_output_limit(host._compaction, selected) in {256, 512}
    assert complete_cost(selected) <= remaining < complete_cost(original)
    assert selected.plan.pinned_item_ids == (limited.turns[0].items[0].item_id,)
    assert set(item.item_id for item in limited.turns[0].items[-6:]) <= set(
        selected.plan.retained_item_ids
    )
    assert_paired_partition(selected)
    assert replay_compaction_plan(limited, selected.plan) == selected
    assert limited.model_dump_json() == before
    assert not main.requests and not summary.requests


async def test_cancel_between_capacity_candidates_does_not_reach_any_provider(
    tmp_path, monkeypatch
):
    host, main, summary = preflight_host(tmp_path)
    token = CancelToken()
    calls = []

    async def observe(*args, **kwargs):
        calls.append(args[3])
        if len(calls) == 1:
            token.cancel()
            raise KernelError("context_compaction_retained_overflow", "首容量不满足保留边界")
        return await plan_compaction(*args, **kwargs)

    monkeypatch.setattr("harnessix.agent.model_history_runtime.plan_compaction", observe)
    with pytest.raises(TurnCancelled):
        await preflight(host, pressure_thread(), token=token)
    assert len(calls) == 2
    assert not main.requests and not summary.requests


async def test_unsafe_history_is_not_retried_with_a_different_capacity(tmp_path, monkeypatch):
    host, main, summary = preflight_host(tmp_path)
    calls = []

    async def unsafe(*args, **kwargs):
        calls.append(args[3])
        raise KernelError("context_compaction_invalid_history", "开放调用组")

    monkeypatch.setattr("harnessix.agent.model_history_runtime.plan_compaction", unsafe)
    with pytest.raises(KernelError) as caught:
        await preflight(host, pressure_thread())
    assert caught.value.code == "context_compaction_invalid_history"
    assert len(calls) == 1
    assert not main.requests and not summary.requests


def settled_attempt(*, step=1, index=1, completeness="complete", outcome="completed"):
    now = utc_now()
    return ModelAttempt(
        **attempt_start(step=step, index=index).model_dump(exclude={"type"}),
        actual_model="fixture-model",
        response_id="fixture-response",
        status=outcome,
        usage=UsageObservation(
            completeness=completeness,
            **({} if completeness == "unknown" else {"input_tokens": 10, "output_tokens": 3}),
        ),
        started_at=now,
        finished_at=now,
        error=None
        if outcome == "completed"
        else AgentFailure(code="offline_failure", message="离线"),
    )


@pytest.fixture
async def summary_record():
    prepared = await pressure_plan(pressure_thread())
    attempt = settled_attempt(step=prepared.plan.model_step, outcome="failed")
    return CompactionRecord(
        plan=prepared.plan,
        status="failed",
        input_tokens_before=0,
        output_tokens_before=0,
        attempt=attempt,
        failure=attempt.error,
        created_event_sequence=prepared.plan.source_event_sequence + 1,
        finished_event_sequence=prepared.plan.source_event_sequence + 2,
        created_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


@pytest.mark.parametrize("main_steps", [13, 14, 15])
def test_summary_and_next_main_share_the_original_sixteen_slots(summary_record, main_steps):
    turn = (
        pressure_thread()
        .turns[0]
        .model_copy(update={"model_steps": main_steps, "compactions": (summary_record,)})
    )
    if main_steps == 15:
        with pytest.raises(KernelError) as caught:
            require_request_slots(turn)
        assert caught.value.code == "budget_exceeded"
        return
    assert require_request_slots(turn) == 16 - main_steps - 1
    if main_steps == 13:
        assert require_request_slots(turn, 2) == 2
    elif main_steps == 14:
        with pytest.raises(KernelError) as caught:
            require_request_slots(turn, 2)
        assert caught.value.code == "budget_exceeded"


def test_sixteenth_pending_main_intent_is_allowed_but_its_retry_is_not():
    turn = (
        pressure_thread()
        .turns[0]
        .model_copy(update={"model_steps": 16, "status": TurnStatus.CALLING_MODEL})
    )
    assert require_request_slots(turn) == 1
    turn = turn.model_copy(update={"model_attempts": (settled_attempt(step=16, outcome="failed"),)})
    with pytest.raises(KernelError) as caught:
        require_request_slots(turn)
    assert caught.value.code == "budget_exceeded"


@pytest.mark.parametrize(
    "kind", ["main-unknown", "summary-partial", "summary-unknown", "no-intent"]
)
def test_unknown_charge_blocks_main_and_summary_slots(summary_record, kind):
    turn = pressure_thread().turns[0]
    if kind == "main-unknown":
        turn = turn.model_copy(
            update={
                "model_attempts": (settled_attempt(completeness="unknown", outcome="failed"),),
            }
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
        turn = turn.model_copy(update={"compactions": (record,)})
    with pytest.raises(KernelError, match="未知") as caught:
        require_request_slots(turn, 2)
    assert caught.value.code == "budget_exceeded"


async def test_cancelled_planning_never_enters_summary_provider(tmp_path: Path):
    host, main, summary = preflight_host(tmp_path)
    token = CancelToken()
    token.cancel()
    with pytest.raises(TurnCancelled):
        await preflight(host, pressure_thread(), token=token)
    assert not main.requests and not summary.requests


@pytest.mark.parametrize("with_instructions", [False, True])
def test_context_without_source_drift_allows_history_and_catalog_lengths_to_change(
    tmp_path: Path, with_instructions: bool
):
    fragments = (
        (
            ContextFragment(
                kind=ContextFragmentKind.RUNTIME_INSTRUCTION, source="runtime", content="约束"
            ),
        )
        if with_instructions
        else ()
    )
    engine = ContextEngine(
        ContextLimits(context_window_tokens=65_536, reserved_output_tokens=2048), fragments
    )
    original = request(tmp_path).model_copy(
        update={
            "history_documents": ("history" * 200,),
            "tool_documents": ("catalog" * 20,),
        }
    )
    before = engine.prepare(original)
    after = engine.prepare(
        original.model_copy(
            update={
                "history_documents": ("short",),
                "tool_documents": ("short catalog",),
            }
        )
    )
    require_context_unchanged(before, after)
    assert before.inspection.history_tokens != after.inspection.history_tokens
    assert before.inspection.tool_tokens != after.inspection.tool_tokens


@pytest.fixture
async def sourced_context(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("原始项目权限与边界。", encoding="utf-8")
    engine = multi_source_engine(
        tmp_path, ProjectInstructionSource(tmp_path), EnvironmentContextSource(tmp_path)
    )
    original = request(tmp_path).model_copy(update={"history_documents": ("history" * 200,)})
    before = await engine.prepare(original, CancelToken())
    after = await engine.prepare(
        original.model_copy(update={"history_documents": ("short",)}), CancelToken()
    )
    return before, after


def test_stable_real_sources_and_consistency_allow_normal_history_shrink(sourced_context):
    before, after = sourced_context
    require_context_unchanged(before, after)
    assert before.inspection.history_tokens > after.inspection.history_tokens
    assert before.instructions == after.instructions


@pytest.mark.parametrize(
    "field", ["instruction_fingerprint", "limits", "fragments", "sources", "consistency"]
)
def test_context_fact_drift_always_fails_closed(sourced_context, field):
    before, stable = sourced_context
    data = stable.model_dump()
    inspection = data["inspection"]
    if field == "instruction_fingerprint":
        data["instructions"] += " "
        inspection["instruction_fingerprint"] = hashlib.sha256(
            data["instructions"].encode()
        ).hexdigest()
        inspection["instruction_tokens"] += 1
        inspection["estimated_input_tokens"] += 1
    elif field == "limits":
        inspection["limits"]["reserved_output_tokens"] += 1
        inspection["available_input_tokens"] -= 1
    elif field == "fragments":
        inspection["fragments"][0]["priority"] -= 1
    elif field == "sources":
        inspection["sources"][0]["source_revision"] = "a" * 64
    else:
        inspection["spec_version"] = "harnessix.context-inspection/v2"
        del inspection["consistency"]
    changed = PreparedContext.model_validate(data)
    with pytest.raises(KernelError) as caught:
        require_context_unchanged(before, changed)
    assert caught.value.code == "context_compaction_source_changed"
