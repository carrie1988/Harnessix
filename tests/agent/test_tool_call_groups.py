"""宿主工具互斥合同、完整流后原子门禁与SQLite重放；仅使用离线Provider。"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ApprovalRequestContent,
    Budget,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    TurnStatus,
    Usage,
)
from harnessix.agent.ports import ModelToolSelection
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.tool_call_groups import (
    require_exclusive_tool_calls,
    validate_exclusive_tool_groups,
)
from harnessix.agent.tool_rejections import require_closed_rejections
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.context.compaction_runtime_contracts import CompactionRuntimeConfig
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_pressure_compaction_loop import (
    BudgetSummaryProvider,
    investigation_responses,
)
from tests.agent.test_tool_rejection_runtime import rejected_step
from tests.context.test_compaction_runtime import SequenceProvider

GROUPS = (("Patch", "Test"),)


class TupleSubclass(tuple):
    pass


class StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "groups",
    [
        None,
        False,
        1,
        "Patch,Test",
        [],
        {"Patch": "Test"},
        TupleSubclass(GROUPS),
        (None,),
        ("Patch",),
        (["Patch", "Test"],),
        (TupleSubclass(GROUPS[0]),),
        ((),),
        (("Patch",),),
        (("Patch", "Patch"),),
        (("Patch", "Test", "Patch"),),
        (("Patch", None),),
        (("Patch", False),),
        (("Patch", 1),),
        (("Patch", ["Test"]),),
        (("Patch", StringSubclass("Test")),),
        (("Patch", "Hidden"),),
        (("Patch", ""),),
        (("Patch", "x" * 257),),
        GROUPS * 2,
        (("Patch", "Test"), ("Test", "Patch")),
    ],
)
def test_invalid_group_contract_is_strict_and_has_fixed_error(groups):
    # 即使空名、超长名出现在目录中，也不是Provider可调用的有效名称。
    with pytest.raises(KernelError) as error:
        validate_exclusive_tool_groups(groups, {"Read", "Patch", "Test", "", "x" * 257})
    assert error.value.code == "model_tool_view_invalid"
    assert error.value.message == "模型工具互斥组契约无效"


@pytest.mark.parametrize(
    "groups",
    [(), GROUPS, (("Read", "Patch", "Test"),), (("Read", "Patch"), ("Patch", "Test"))],
)
def test_valid_groups_allow_overlap_and_empty_default(groups):
    validate_exclusive_tool_groups(groups, {"Read", "Patch", "Test"})
    assert ModelToolSelection(()).exclusive_tool_groups == ()


def test_valid_tool_name_boundaries():
    groups = (("x", "y" * 256),)
    validate_exclusive_tool_groups(groups, {"x", "y" * 256})
    require_exclusive_tool_calls(["y" * 256, "y" * 256], groups)


class GroupTools(RecordingTools):
    def __init__(self, *, names=("Read", "Patch", "Test"), **kwargs):
        super().__init__(**kwargs)
        self.names = names

    def definitions(self):
        base = super().definitions()[0]
        return tuple(base.model_copy(update={"name": name}) for name in self.names)


class GroupView:
    def __init__(self, groups=GROUPS, *, visible=None):
        self.groups = groups
        self.visible = visible

    def select(self, turn, definitions):
        selected = tuple(d for d in definitions if self.visible is None or d.name in self.visible)
        return ModelToolSelection(selected, exclusive_tool_groups=self.groups)


@pytest.mark.parametrize(
    "groups,visible",
    [
        ([GROUPS[0]], None),
        ((["Patch", "Test"],), None),
        ((("Patch", None),), None),
        (GROUPS * 2, None),
        ((("Patch", "Test"), ("Test", "Patch")), None),
        (GROUPS, ("Read", "Test")),
        (GROUPS, ()),
        ((("Read", "x" * 257),), None),
        ((("Read", ""),), None),
    ],
)
async def test_invalid_selection_fails_before_provider(tmp_path, groups, visible):
    tools = GroupTools(names=("Read", "Patch", "Test", "", "x" * 257))
    provider = ScriptedProvider([answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "invalid.sqlite"),
        provider,
        tools,
        model_tool_view=GroupView(groups, visible=visible),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "非法规则", request_id="invalid")
    assert turn.status == TurnStatus.FAILED and turn.error.code == "model_tool_view_invalid"
    assert provider.requests == [] and provider.closed_streams == 0 and tools.calls == []


def response_with_usage(*names, accounted=False, usage=None):
    usage = usage or Usage(input_tokens=10, output_tokens=3)
    events = tool_step(*names)
    for index, event in enumerate(events):
        if isinstance(event, ToolCallCompleted):
            events[index] = event.model_copy(update={"arguments": {"private": "PRIVATE-BODY"}})
    events[-1] = ResponseCompleted(finish_reason="tool_calls", usage=usage)
    if not accounted:
        return events
    start = attempt_start()
    events[0] = ResponseStarted(response_id="response-1")
    return [
        start,
        *events[:-1],
        observed(
            start,
            completeness="complete",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=min(4, usage.input_tokens),
            reasoning_output_tokens=min(1, usage.output_tokens),
        ),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        events[-1],
    ]


@pytest.mark.parametrize("names", [("Read", "Patch", "Test"), ("Test", "Patch", "Read")])
@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
@pytest.mark.parametrize("approval", [False, True], ids=["no-approval", "approval"])
@pytest.mark.parametrize("accounted", [False, True], ids=["legacy-usage", "attempt-usage"])
async def test_conflict_has_no_execution_approval_or_recoverable_calls(
    tmp_path, names, parallel, approval, accounted
):
    tools = GroupTools(parallel=parallel, approval=approval)
    provider = ScriptedProvider([response_with_usage(*names, accounted=accounted)])
    store = SQLiteSessionStore(tmp_path / "conflict.sqlite")
    async with AgentRuntime(
        store, provider, tools, max_parallel_tools=4 if parallel else 1, model_tool_view=GroupView()
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "互斥门禁", request_id="conflict")

    assert turn.status == TurnStatus.FAILED and turn.error.code == "invalid_provider_output"
    assert turn.error.message == "模型响应包含互斥工具调用" and not turn.error.retryable
    assert tools.calls == [] and provider.closed_streams == len(provider.requests) == 1
    assert turn.usage == Usage(input_tokens=10, output_tokens=3)
    if accounted:
        attempt = turn.model_attempts[0]
        assert attempt.status == "completed" and attempt.usage.completeness == "complete"
        assert attempt.usage.cache_read_input_tokens == 4
        assert attempt.usage.reasoning_output_tokens == 1
    assert not pending_calls(turn)
    assert not any(
        isinstance(
            item.content, ToolCallContent | ToolCallRejectionContent | ApprovalRequestContent
        )
        for item in turn.items
    )
    reopened = SQLiteSessionStore(store.path)
    restored = await reopened.get_thread(thread.thread_id)
    events = await reopened.events(thread.thread_id)
    assert replay(events) == restored and restored.turns[-1] == turn
    assert "PRIVATE-BODY" not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("names", [("Read", "Test"), ("Patch",), ("Test",), ("Test", "Test")])
async def test_allowed_calls_and_host_rules_are_not_advertised(tmp_path, names):
    tools = GroupTools(parallel=True)
    provider = ScriptedProvider([tool_step(*names), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "allowed.sqlite"),
        provider,
        tools,
        model_tool_view=GroupView(),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "正常调用", request_id="allowed")
        selected = runtime._select_model_tools(turn, tools.definitions())
    assert turn.status == TurnStatus.COMPLETED
    assert sorted(call.tool for call in tools.calls) == sorted(names)
    assert selected.exclusive_tool_groups == GROUPS
    assert selected != replace(selected, exclusive_tool_groups=())
    for request in provider.requests:
        assert request.tools == tools.definitions() and request.instructions is None
        assert not hasattr(request, "exclusive_tool_groups")
        assert "exclusive_tool_groups" not in request.model_dump_json()


@pytest.mark.parametrize("view_kind", ["none", "tuple", "selection"])
async def test_empty_rules_keep_legacy_conflicting_calls_executable(tmp_path, view_kind):
    class TupleView:
        def select(self, turn, definitions):
            return definitions

    view = None if view_kind == "none" else TupleView() if view_kind == "tuple" else GroupView(())
    tools = GroupTools()
    provider = ScriptedProvider([tool_step("Patch", "Test"), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "legacy.sqlite"), provider, tools, model_tool_view=view
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "旧行为", request_id="legacy")
    assert turn.status == TurnStatus.COMPLETED
    assert [call.tool for call in tools.calls] == ["Patch", "Test"]


async def test_selection_keeps_groups_choice_and_forced_tool_when_reordering(tmp_path):
    class ForcedView:
        def select(self, turn, definitions):
            return ModelToolSelection(tuple(reversed(definitions)), "required", "Test", GROUPS)

    tools = GroupTools()
    provider = ScriptedProvider([tool_step("Test")])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "selection.sqlite"),
        provider,
        tools,
        model_tool_view=ForcedView(),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "强制选择", request_id="forced", budget=Budget(max_steps=1)
        )
        selected = runtime._select_model_tools(turn, tools.definitions())
    assert selected == ModelToolSelection(tools.definitions(), "required", "Test", GROUPS)
    assert provider.requests[0].tool_choice == "required"
    assert provider.requests[0].forced_tool == "Test" and len(tools.calls) == 1


@pytest.mark.parametrize("unknown_first", [False, True])
async def test_unknown_and_rejected_proposals_keep_non_authoritative_feedback(
    tmp_path, unknown_first
):
    events = tool_step("Test", "Hidden", "Unknown")
    events.insert(1, rejected_step()[1])
    if unknown_first:
        events[1:-1] = reversed(events[1:-1])
    tools = GroupTools(names=("Read", "Patch", "Test", "Hidden"))
    provider = ScriptedProvider([events, answer()])
    store = SQLiteSessionStore(tmp_path / "unknown.sqlite")
    async with AgentRuntime(
        store,
        provider,
        tools,
        model_tool_view=GroupView(visible=("Read", "Patch", "Test")),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "无权提案", request_id="unknown")
    assert turn.status == TurnStatus.COMPLETED and [call.tool for call in tools.calls] == ["Test"]
    rejections = [i.content for i in turn.items if isinstance(i.content, ToolCallRejectionContent)]
    rejected_ids = {content.call_id for content in rejections}
    results = [
        i.content
        for i in turn.items
        if isinstance(i.content, ToolResultContent) and i.content.call_id in rejected_ids
    ]
    assert len(rejections) == len(results) == 3
    assert all(result.error.code == "unknown_tool" and result.output is None for result in results)
    require_closed_rejections(await store.get_thread(thread.thread_id))


@pytest.mark.parametrize(
    "tail,code",
    [
        ("incomplete", "provider_stream_incomplete"),
        ("after-terminal", "invalid_provider_output"),
        ("usage-mismatch", "invalid_provider_output"),
        ("exact-budget", "budget_exceeded"),
        ("over-budget", "budget_exceeded"),
    ],
)
async def test_stream_integrity_and_accounted_budget_precede_group_gate(tmp_path, tail, code):
    usage = Usage(input_tokens=10, output_tokens=3)
    if tail in {"exact-budget", "over-budget"}:
        usage = Usage(input_tokens=50_000, output_tokens=int(tail == "over-budget"))
    events = response_with_usage("Patch", "Test", accounted=True, usage=usage)
    if tail == "incomplete":
        events.pop()
    elif tail == "after-terminal":
        events.append(ToolCallCompleted(call_id="late", tool="Read"))
    elif tail == "usage-mismatch":
        events[-1] = ResponseCompleted(finish_reason="tool_calls", usage=Usage(input_tokens=99))
    tools = GroupTools()
    provider = ScriptedProvider([events])
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "tail.sqlite"), provider, tools, model_tool_view=GroupView()
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "完整流与预算优先",
            request_id="tail",
            budget=Budget(max_tokens=50_000, max_output_chars=1024),
        )
    assert turn.status == TurnStatus.FAILED and turn.error.code == code
    assert turn.error.message != "模型响应包含互斥工具调用"
    assert turn.usage == usage and turn.model_attempts[0].status == "completed"
    assert provider.closed_streams == 1 and tools.calls == [] and not pending_calls(turn)
    assert not any(isinstance(i.content, ToolCallContent) for i in turn.items)


async def test_interleaved_requests_and_later_turns_do_not_share_group_rules(tmp_path):
    class RequestView:
        def select(self, turn, definitions):
            return ModelToolSelection(
                definitions, exclusive_tool_groups=GROUPS if turn.request_id == "gated" else ()
            )

    class PausingProvider(ScriptedProvider):
        def __init__(self):
            super().__init__([tool_step("Patch", "Test"), answer()])
            self.paused = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(self, request, cancel):
            async with aclosing(super().stream(request, cancel)) as stream:
                async for event in stream:
                    yield event
                    if request.turn_id == gated_turn_id and isinstance(event, ToolCallCompleted):
                        self.paused.set()
                        await self.release.wait()

    tools = GroupTools()
    provider = PausingProvider()
    store = SQLiteSessionStore(tmp_path / "interleaved.sqlite")
    async with AgentRuntime(store, provider, tools, model_tool_view=RequestView()) as runtime:
        gated_thread = await runtime.create_thread(str(tmp_path / "gated"))
        open_thread = await runtime.create_thread(str(tmp_path / "open"))
        gated_turn = await runtime.accept_turn(gated_thread.thread_id, "gated", request_id="gated")
        gated_turn_id = gated_turn.turn_id
        task = asyncio.create_task(runtime.resume_turn(gated_thread.thread_id, gated_turn_id))
        try:
            await asyncio.wait_for(provider.paused.wait(), 3)
            open_turn = await runtime.run_turn(open_thread.thread_id, "open", request_id="open")
            provider.release.set()
            gated_result = await asyncio.wait_for(task, 3)
        finally:
            provider.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        later = await runtime.run_turn(gated_thread.thread_id, "later", request_id="later")
    assert gated_result.status == TurnStatus.FAILED
    assert gated_result.error.code == "invalid_provider_output"
    assert open_turn.status == later.status == TurnStatus.COMPLETED
    assert [call.tool for call in tools.calls] == ["Patch", "Test", "Patch", "Test"]
    assert not pending_calls(gated_result)


@pytest.mark.parametrize("drift", [False, True], ids=["retained", "changed"])
async def test_compaction_comparison_includes_host_groups(tmp_path, drift):
    class CompactionView:
        def select(self, turn, definitions):
            return ModelToolSelection(
                definitions,
                exclusive_tool_groups=()
                if drift and turn.compactions
                else (("test.read", "other"),),
            )

    tools = GroupTools(names=("test.read", "other"))
    main = SequenceProvider(investigation_responses(9))
    summary = BudgetSummaryProvider([])
    config = CompactionRuntimeConfig(
        policy=CompactionPolicy(
            target_history_tokens=65536,
            summary_reserve_tokens=12288,
            max_summary_input_tokens=262144,
            retain_recent_groups=2,
        ),
        trigger_history_tokens=131072,
        max_summary_output_tokens=2048,
    )
    store = SQLiteSessionStore(tmp_path / "compaction.sqlite")
    async with AgentRuntime(
        store,
        main,
        tools,
        model_tool_view=CompactionView(),
        compaction=config,
        summary_provider=summary,
        budget_pressure_compaction=True,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id,
            "压缩约束快照",
            request_id="compaction",
            budget=Budget(max_tokens=50_000, max_steps=16),
        )
    assert summary.requests and all(not request.tools for request in summary.requests)
    assert all(
        "exclusive_tool_groups" not in request.model_dump_json() for request in main.requests
    )
    if drift:
        assert turn.status == TurnStatus.FAILED
        assert turn.error.code == "context_compaction_source_changed"
        assert len(summary.requests) == 1 and len(tools.calls) == len(main.requests) < 10
    else:
        assert turn.status == TurnStatus.COMPLETED and len(tools.calls) == 9
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
