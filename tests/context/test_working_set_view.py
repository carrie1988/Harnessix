"""工作集模型视图：只保留最近调用组结果正文，确定性、可回放、不改Native事实。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.event_compatibility import validate_event_boundary
from harnessix.agent.models import (
    Budget,
    EventDraft,
    Item,
    ItemStatus,
    ModelHistoryPrepared,
    TextContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_rejections import rejection_result
from harnessix.context.tool_result_contracts import (
    STALE_TOOL_RESULT_VERSION,
    ModelHistoryInspection,
    ModelHistoryInspectionV3,
)
from harnessix.context.tool_result_view import (
    PreparedModelHistory,
    view_history_sha256,
)
from harnessix.context.working_set_view import (
    apply_working_set,
    prepare_model_view,
    previous_model_view,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import require_projection_content_version
from tests.agent.helpers import answer
from tests.agent.test_provider_input_budget import WireProvider
from tests.agent.test_rewritten_input_budget import LargeReadTools, RewritingProvider, read_step
from tests.models.test_chat_mapping import call

SHA = "a" * 64


def done(content) -> Item:
    return Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)


def read_output(index: int) -> dict[str, object]:
    return {
        "path": f"src/module_{index}.py",
        "start_line": 1,
        "revision": index,
        "content_sha256": SHA,
        "text": "源码\n" * 400,
    }


def history_with_groups(*group_sizes: int) -> tuple[Item, ...]:
    items = [done(TextContent(kind="user_message", text="任务"))]
    for group, size in enumerate(group_sizes):
        calls = [call() for _ in range(size)]
        items.extend(done(c) for c in calls)
        items.extend(
            done(ToolResultContent(call_id=c.call_id, outcome="succeeded", output=read_output(i)))
            for i, c in enumerate(calls, start=group * 10)
        )
    return tuple(items)


def base_view(history: tuple[Item, ...]) -> PreparedModelHistory:
    results = sum(isinstance(item.content, ToolResultContent) for item in history)
    inspection = ModelHistoryInspection(
        model_step=len(history),
        policy={},
        history_items=len(history),
        tool_results=results,
        inline_results=results,
        artifact_reference_results=0,
        artifact_bindings=0,
        source_tool_result_utf8_bytes=0,
        view_tool_result_utf8_bytes=0,
        source_history_sha256=SHA,
        view_history_sha256=view_history_sha256(history),
        decisions_sha256=SHA,
    )
    return PreparedModelHistory(history, inspection, (), ())


def outputs(history: tuple[Item, ...]) -> list[object]:
    return [item.content.output for item in history if isinstance(item.content, ToolResultContent)]


def test_only_results_outside_recent_call_groups_are_replaced():
    history = history_with_groups(2, 1, 1)
    prepared = apply_working_set(base_view(history), 2)
    first, second, third, fourth = outputs(prepared.history)
    assert first["spec_version"] == second["spec_version"] == STALE_TOOL_RESULT_VERSION
    assert third == read_output(10) and fourth == read_output(20)
    assert prepared.inspection.stale_results == 2


def test_placeholder_keeps_short_scalars_and_never_long_text():
    prepared = apply_working_set(base_view(history_with_groups(1, 1)), 1)
    stale = outputs(prepared.history)[0]
    assert stale["retained"] == {
        "content_sha256": SHA,
        "path": "src/module_0.py",
        "revision": 0,
        "start_line": 1,
    }
    assert stale["source_utf8_bytes"] > 1000 and "text" not in stale["retained"]


def test_native_items_identity_and_order_are_unchanged():
    history = history_with_groups(1, 1, 1)
    prepared = apply_working_set(base_view(history), 1)
    assert [i.item_id for i in prepared.history] == [i.item_id for i in history]
    assert [type(i.content) for i in prepared.history] == [type(i.content) for i in history]
    assert outputs(history)[0] == read_output(0)


def test_small_outputs_and_rejected_calls_are_not_rewritten():
    rejected_id = uuid4()
    small = call()
    history = (
        done(TextContent(kind="user_message", text="任务")),
        done(ToolCallRejectionContent(call_id=rejected_id, provider_call_id="x", model_step=1)),
        done(rejection_result(rejected_id)),
        done(small),
        done(ToolResultContent(call_id=small.call_id, outcome="succeeded", output={"v": 1})),
        *history_with_groups(1)[1:],
    )
    prepared = apply_working_set(base_view(history), 1)
    assert outputs(prepared.history)[:2] == outputs(history)[:2]
    assert prepared.inspection.stale_results == 0


def test_inspection_binds_actual_view_and_preserves_base_facts():
    base = base_view(history_with_groups(1, 1))
    prepared = apply_working_set(base, 1)
    inspection = prepared.inspection
    assert isinstance(inspection, ModelHistoryInspectionV3)
    assert inspection.view_history_sha256 == view_history_sha256(prepared.history)
    assert inspection.view_history_sha256 != base.inspection.view_history_sha256
    assert inspection.model_dump(
        exclude={
            "spec_version",
            "view_history_sha256",
            "retain_recent_result_groups",
            "stale_results",
        }
    ) == base.inspection.model_dump(exclude={"spec_version", "view_history_sha256"})


def test_projection_is_deterministic():
    history = history_with_groups(2, 1, 1)
    assert apply_working_set(base_view(history), 1) == apply_working_set(base_view(history), 1)


def test_v3_inspection_requires_event_v22_and_projection_v22():
    inspection = apply_working_set(base_view(history_with_groups(1, 1)), 1).inspection
    with pytest.raises(ValueError):
        validate_event_boundary(
            EventDraft(schema_version=21, payload=ModelHistoryPrepared(inspection=inspection))
        )
    validate_event_boundary(EventDraft(payload=ModelHistoryPrepared(inspection=inspection)))


async def run_working_set(tmp_path, provider, groups):
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        LargeReadTools(),
        working_set_result_groups=groups,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        return await runtime.run_turn(thread.thread_id, "任务", request_id="ws")


async def stored_thread(tmp_path, provider):
    return await SQLiteSessionStore(tmp_path / "session.db").get_thread(
        provider.requests[0].thread_id
    )


async def test_runtime_sends_masked_view_and_replay_verifies_it(tmp_path):
    provider = RewritingProvider([read_step(1, 10), read_step(2, 20), read_step(3, 30), answer()])
    turn = await run_working_set(tmp_path, provider, 1)
    assert turn.status is TurnStatus.COMPLETED
    last = provider.requests[-1]
    stale = [o for o in outputs(last.history) if o.get("spec_version") == STALE_TOOL_RESULT_VERSION]
    assert len(stale) == 2 and outputs(last.history)[-1]["text"]
    # 重新打开Session会重放全部事件，并由Reducer按V3记录重算工作集视图。
    store = SQLiteSessionStore(tmp_path / "session.db")
    restored = await store.get_thread(last.thread_id)
    inspections = restored.turns[0].model_history_inspections
    assert all(isinstance(i, ModelHistoryInspectionV3) for i in inspections)
    assert all(event.schema_version == 22 for event in await store.events(last.thread_id))
    require_projection_content_version(restored, 22)
    with pytest.raises(KernelError):
        require_projection_content_version(restored, 21)


async def test_previous_view_is_recomputed_exactly_from_persisted_facts(tmp_path):
    provider = RewritingProvider([read_step(1, 10), read_step(2, 20), read_step(3, 30), answer()])
    await run_working_set(tmp_path, provider, 1)
    thread = await stored_thread(tmp_path, provider)
    for inspection, sent in zip(
        thread.turns[0].model_history_inspections, provider.requests, strict=True
    ):
        assert previous_model_view(thread, inspection) == sent.history


async def test_disabled_working_set_matches_original_view(tmp_path):
    provider = RewritingProvider([read_step(1, 10), read_step(2, 20), answer()])
    await run_working_set(tmp_path, provider, None)
    thread = await stored_thread(tmp_path, provider)
    inspections = thread.turns[0].model_history_inspections
    assert all(type(i) is ModelHistoryInspection for i in inspections)
    assert all(
        output == {"text": "源码行\n" * 1500}
        for request in provider.requests
        for output in outputs(request.history)
    )
    previous = thread.turns[0].model_history_inspections[-1]
    assert prepare_model_view(thread, 1, previous.policy, None).history[:1] == (
        provider.requests[0].history
    )


def admitted(request):
    """准入在预付最长预算说明的候选请求上计算，正式请求随后换成较短实际上界。"""
    return request, request.model_copy(
        update={"input_tokens_upper_bound": request.budget.max_tokens}
    )


async def run_budgeted(tmp_path, provider, groups, budget):
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        LargeReadTools(),
        working_set_result_groups=groups,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        return await runtime.run_turn(thread.thread_id, "任务", request_id="ws", budget=budget)


async def test_admission_anchors_rewritten_view_on_previous_actual_usage(tmp_path):
    provider = RewritingProvider([read_step(1, 10), read_step(2, 20), read_step(3, 30), answer()])
    await run_working_set(tmp_path, provider, 1)
    last, candidate = admitted(provider.requests[-1])
    added = provider.adapter.estimate_rewritten_input_tokens(
        candidate, provider.requests[-2].history
    )
    assert last.input_tokens_upper_bound == 30 + added
    assert last.input_tokens_upper_bound < provider.adapter.estimate_input_tokens(candidate)


async def test_rewritten_view_without_capability_pays_full_wire(tmp_path):
    provider = WireProvider([read_step(1, 10), read_step(2, 20), read_step(3, 30), answer()])
    await run_working_set(tmp_path, provider, 1)
    last, candidate = admitted(provider.requests[-1])
    # 遮蔽改写了前缀，旧前缀锚点不再成立；没有改写端口时必须回退完整出站上界。
    assert last.input_tokens_upper_bound == provider.adapter.estimate_input_tokens(candidate)


async def test_actual_input_above_rewritten_bound_fails_closed(tmp_path):
    provider = RewritingProvider(
        [read_step(1, 10), read_step(2, 20), read_step(3, 900_000), answer()]
    )
    turn = await run_budgeted(tmp_path, provider, 1, Budget(max_tokens=5_000_000))
    assert turn.status is TurnStatus.FAILED
    assert turn.error.code == "model_input_bound_violated"
    assert len(provider.requests) == 3
