"""内置计划工具的离线契约；模型自报步骤不是测试或任务验收事实。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import JsonValue

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    Budget,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PlanContent,
    PlanStep,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStateChanged,
    TurnStatus,
    Usage,
)
from harnessix.agent.planning import _decode_plan
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    ToolCallCompleted,
    ToolCallRejected,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start, observed
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_publication import CANARY, protected


def plan_arguments(*steps: PlanStep) -> dict[str, JsonValue]:
    steps = steps or (PlanStep(step_id="read", description="检查代码"),)
    return {"steps": [step.model_dump(mode="json") for step in steps]}


def plan_call(
    arguments: dict[str, JsonValue] | None = None, *, call_id: str = "plan"
) -> ToolCallCompleted:
    return ToolCallCompleted(
        call_id=call_id,
        tool="update_plan",
        arguments=plan_arguments() if arguments is None else arguments,
    )


def proposal_step(
    *calls: ToolCallCompleted | ToolCallRejected, usage: Usage | None = None
) -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="response"),
        *calls,
        ResponseCompleted(finish_reason="tool_calls", usage=usage or Usage()),
    ]


def plan_items(turn: Turn) -> list[Item]:
    return [item for item in turn.items if isinstance(item.content, PlanContent)]


def result_for(turn: Turn, provider_call_id: str = "plan") -> ToolResultContent:
    calls = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
        and item.content.provider_call_id == provider_call_id
    ]
    assert len(calls) == 1
    results = [
        item.content
        for item in turn.items
        if isinstance(item.content, ToolResultContent) and item.content.call_id == calls[0].call_id
    ]
    assert len(results) == 1
    return results[0]


async def run_script(
    tmp_path: Path,
    steps: Sequence[Sequence[ProviderEvent]],
    *,
    tools: RecordingTools | None = None,
    budget: Budget | None = None,
) -> tuple[SQLiteSessionStore, ScriptedProvider, Turn]:
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider(steps)
    async with AgentRuntime(store, provider, tools, enable_planning=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(
            thread.thread_id, "离线计划契约", request_id="r", budget=budget
        )
    restored = await store.get_thread(thread.thread_id)
    assert replay(await store.events(thread.thread_id)) == restored
    assert all(item.status != ItemStatus.STARTED for item in turn.items)
    return store, provider, turn


@pytest.mark.parametrize("explicit_false", [False, True], ids=["default", "explicit-false"])
async def test_disabled_preserves_catalog_and_creates_no_plan(tmp_path, explicit_false):
    tools = RecordingTools()
    provider = ScriptedProvider([proposal_step(plan_call()), tool_step("test.read"), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    options = {"enable_planning": False} if explicit_false else {}
    async with AgentRuntime(store, provider, tools, **options) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "保持旧目录", request_id="r")
    assert turn.status is TurnStatus.COMPLETED
    assert all(request.tools == tools.definitions() for request in provider.requests)
    assert not plan_items(turn)
    assert [call.tool for call in tools.calls] == ["test.read"]
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert results[0].outcome == "failed" and results[0].error.code == "unknown_tool"
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_enabled_descriptor_and_host_only_bounded_result(tmp_path):
    sentinel = tmp_path / "unchanged.txt"
    sentinel.write_text("不授予文件权限", encoding="utf-8")
    tools = RecordingTools()
    arguments = plan_arguments(PlanStep(step_id="one", description="不可在结果重复的计划正文"))
    _, provider, turn = await run_script(
        tmp_path, [proposal_step(plan_call(arguments)), answer()], tools=tools
    )
    assert turn.status is TurnStatus.COMPLETED and turn.model_steps == 2
    descriptor = next(tool for tool in provider.requests[0].tools if tool.name == "update_plan")
    assert descriptor.version == "harnessix.update-plan/v1"
    assert descriptor.effect_class is EffectClass.READ_ONLY
    assert descriptor.risk_level is RiskLevel.LOW
    assert not descriptor.supports_parallel_calls
    assert not descriptor.requires_approval and not descriptor.requires_idempotency
    assert not descriptor.supports_reconciliation
    assert descriptor.input_schema["additionalProperties"] is False
    schema = descriptor.input_schema["properties"]["steps"]
    assert schema["type"] == "array" and schema["minItems"] == 1 and schema["maxItems"] == 8
    plans = plan_items(turn)
    assert len(plans) == 1 and plans[0].status is ItemStatus.COMPLETED
    assert plans[0].content == PlanContent(steps=(PlanStep.model_validate(arguments["steps"][0]),))
    result = result_for(turn)
    assert result.outcome == "succeeded" and result.error is None
    assert result.output == {
        "plan_item_id": str(plans[0].item_id),
        "step_count": 1,
        "self_reported": True,
    }
    assert type(result.output["plan_item_id"]) is str
    assert type(result.output["step_count"]) is int
    assert result.output["self_reported"] is True
    assert "不可在结果重复的计划正文" not in result.model_dump_json()
    assert tools.calls == []
    assert sentinel.read_text(encoding="utf-8") == "不授予文件权限"
    assert [path.name for path in tmp_path.iterdir() if path.suffix == ".txt"] == [sentinel.name]


async def test_result_preparing_state_and_plan_are_one_session_append(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "session.db")
    append = AsyncMock(wraps=store.append)
    monkeypatch.setattr(store, "append", append)
    provider = ScriptedProvider([proposal_step(plan_call()), answer()])
    async with AgentRuntime(store, provider, enable_planning=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "原子提交", request_id="r")
    assert turn.status is TurnStatus.COMPLETED
    plan = plan_items(turn)[0]
    result = result_for(turn)
    batches = [call.args[1] for call in append.await_args_list]
    plan_batches = [
        batch
        for batch in batches
        if any(
            isinstance(draft.payload, ItemStarted) and draft.payload.content == plan.content
            for draft in batch
        )
    ]
    assert len(plan_batches) == 1
    payloads = [draft.payload for draft in plan_batches[0]]
    assert len(payloads) == 5
    assert sum(isinstance(p, ItemStarted) and p.content == result for p in payloads) == 1
    assert sum(isinstance(p, ItemFinished) and p.content == result for p in payloads) == 1
    assert sum(isinstance(p, ItemStarted) and p.item_id == plan.item_id for p in payloads) == 1
    assert sum(isinstance(p, ItemFinished) and p.item_id == plan.item_id for p in payloads) == 1
    assert (
        sum(
            isinstance(p, TurnStateChanged) and p.status is TurnStatus.PREPARING_CONTEXT
            for p in payloads
        )
        == 1
    )
    assert {draft.turn_id for draft in plan_batches[0]} == {turn.turn_id}
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_revisions_supersede_latest_and_calls_not_native_plans_enter_history(tmp_path):
    first = plan_arguments(PlanStep(step_id="read", description="阅读代码", status="in_progress"))
    second = plan_arguments(
        PlanStep(step_id="read", description="阅读代码", status="completed"),
        PlanStep(step_id="check", description="验证行为", status="in_progress"),
    )
    store, provider, turn = await run_script(
        tmp_path,
        [
            proposal_step(plan_call(first, call_id="first")),
            proposal_step(plan_call(second, call_id="second")),
            answer(),
        ],
    )
    assert turn.status is TurnStatus.COMPLETED
    plans = plan_items(turn)
    assert len(plans) == 2 and plans[0].item_id != plans[1].item_id
    assert plans[0].content.supersedes is None
    assert plans[1].content.supersedes == plans[0].item_id
    assert plans[0].content.steps[0].status == "in_progress"
    assert plans[1].content.steps[0].status == "completed"
    for index, expected in [(1, [first]), (2, [first, second])]:
        history = provider.requests[index].history
        assert not any(isinstance(item.content, PlanContent) for item in history)
        assert [
            item.content.arguments
            for item in history
            if isinstance(item.content, ToolCallContent) and item.content.tool == "update_plan"
        ] == expected
        assert (
            len([item for item in history if isinstance(item.content, ToolResultContent)]) == index
        )
    restored = await SQLiteSessionStore(store.path).get_thread(provider.requests[0].thread_id)
    assert plan_items(restored.turns[0]) == plans


async def test_revision_after_reopen_uses_host_latest_plan_id(tmp_path):
    store, first_provider, first_turn = await run_script(
        tmp_path, [proposal_step(plan_call()), answer()]
    )
    provider = ScriptedProvider([proposal_step(plan_call()), answer()])
    async with AgentRuntime(
        SQLiteSessionStore(store.path), provider, enable_planning=True
    ) as runtime:
        turn = await runtime.run_turn(
            first_provider.requests[0].thread_id, "继续计划", request_id="r2"
        )
    assert turn.status is TurnStatus.COMPLETED
    assert plan_items(turn)[0].content.supersedes == plan_items(first_turn)[0].item_id
    assert not any(isinstance(item.content, PlanContent) for item in provider.requests[0].history)
    assert any(
        isinstance(item.content, ToolCallContent) and item.content.tool == "update_plan"
        for item in provider.requests[0].history
    )


@pytest.mark.parametrize("order", ["plan-read", "read-plan", "plan-plan", "plan-read-plan"])
async def test_only_last_pending_call_can_update_and_later_read_still_executes(tmp_path, order):
    calls = [
        plan_call(call_id=f"plan-{index}")
        if name == "plan"
        else ToolCallCompleted(call_id="read", tool="test.read")
        for index, name in enumerate(order.split("-"))
    ]
    tools = RecordingTools(parallel=True)
    _, _, turn = await run_script(tmp_path, [proposal_step(*calls), answer()], tools=tools)
    assert turn.status is TurnStatus.COMPLETED
    expected_plans = int(calls[-1].tool == "update_plan")
    assert len(plan_items(turn)) == expected_plans
    assert len(tools.calls) == sum(call.tool == "test.read" for call in calls)
    for index, call in enumerate(calls):
        result = result_for(turn, call.call_id)
        if call.tool == "test.read" or index == len(calls) - 1:
            assert result.outcome == "succeeded"
        else:
            assert result.outcome == "failed" and result.error.code == "tool_invalid_arguments"
    if expected_plans:
        assert result_for(turn, calls[-1].call_id).output["plan_item_id"] == str(
            plan_items(turn)[0].item_id
        )


async def test_already_settled_rejection_after_plan_is_not_a_pending_tail(tmp_path):
    _, _, turn = await run_script(
        tmp_path,
        [
            proposal_step(plan_call(), ToolCallRejected(call_id="unknown", argument_chars=2)),
            answer(),
        ],
    )
    assert turn.status is TurnStatus.COMPLETED and len(plan_items(turn)) == 1
    assert result_for(turn).outcome == "succeeded"
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == 2
    assert (
        sum(
            result.outcome == "failed" and result.error.code == "unknown_tool" for result in results
        )
        == 1
    )


INVALID_ARGUMENTS = [
    pytest.param({}, id="missing-steps"),
    pytest.param({"steps": []}, id="empty-array"),
    pytest.param({"steps": {}}, id="object-not-array"),
    pytest.param({"steps": "DO_NOT_ECHO"}, id="string-not-array"),
    pytest.param({"steps": ["DO_NOT_ECHO"]}, id="non-object-step"),
    pytest.param({"steps": [[{"step_id": "a", "description": "DO_NOT_ECHO"}]]}, id="nested-array"),
    pytest.param(
        {"steps": [{"step_id": "a", "description": "DO_NOT_ECHO"}] * 2}, id="duplicate-ids"
    ),
    pytest.param(
        {
            "steps": [
                {"step_id": str(i), "description": "DO_NOT_ECHO", "status": "in_progress"}
                for i in range(2)
            ]
        },
        id="multiple-in-progress",
    ),
    pytest.param(
        {"steps": [{"step_id": str(i), "description": "DO_NOT_ECHO"} for i in range(9)]},
        id="nine-steps",
    ),
    pytest.param(
        {"steps": [{"step_id": "a", "description": "DO_NOT_ECHO", "status": "done"}]},
        id="unknown-status",
    ),
    pytest.param(
        {"steps": [{"step_id": "a", "description": "DO_NOT_ECHO", "status": None}]},
        id="null-status",
    ),
    pytest.param({"steps": [{"step_id": "", "description": "DO_NOT_ECHO"}]}, id="empty-id"),
    pytest.param({"steps": [{"step_id": "a", "description": ""}]}, id="empty-description"),
    pytest.param({"steps": [{"step_id": 1, "description": "DO_NOT_ECHO"}]}, id="numeric-id"),
    pytest.param({"steps": [{"step_id": "a", "description": True}]}, id="boolean-description"),
    pytest.param(
        {"steps": [{"step_id": "a", "description": "DO_NOT_ECHO", "extra": "bad"}]},
        id="extra-step-field",
    ),
    pytest.param(
        {"steps": [{"step_id": "a", "description": "DO_NOT_ECHO"}], "supersedes": str(UUID(int=1))},
        id="client-supersedes",
    ),
    pytest.param(
        {
            "steps": [{"step_id": "a", "description": "DO_NOT_ECHO"}],
            "plan_item_id": str(UUID(int=2)),
        },
        id="client-plan-id",
    ),
    pytest.param({"steps": [{"step_id": "a\x00", "description": "DO_NOT_ECHO"}]}, id="nul-id"),
    pytest.param(
        {"steps": [{"step_id": "a", "description": "DO_NOT_ECHO\x00"}]}, id="nul-description"
    ),
]


@pytest.mark.parametrize("arguments", INVALID_ARGUMENTS)
async def test_invalid_input_is_sanitized_feedback_and_next_read_remains_usable(
    tmp_path, arguments
):
    tools = RecordingTools()
    _, provider, turn = await run_script(
        tmp_path,
        [proposal_step(plan_call(arguments)), tool_step("test.read"), answer()],
        tools=tools,
    )
    assert turn.status is TurnStatus.COMPLETED and not plan_items(turn)
    result = result_for(turn)
    assert result.outcome == "failed" and result.error.code == "tool_invalid_arguments"
    assert result.output is None and not result.error.retryable
    feedback = result.model_dump_json()
    assert "DO_NOT_ECHO" not in feedback
    assert "ValidationError" not in feedback and "input_value" not in feedback
    assert "errors.pydantic.dev" not in feedback
    assert [call.tool for call in tools.calls] == ["test.read"]
    assert any(
        isinstance(item.content, ToolCallContent) and item.content.arguments == arguments
        for item in provider.requests[1].history
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "\ud800"])
def test_decoder_rejects_non_finite_json_and_invalid_utf8_without_raw_exception(value):
    # 非法UTF-8无法先持久化为Session JSON；直接覆盖真实解析边界，不伪造Store快照。
    call = ToolCallContent(
        call_id=UUID(int=1),
        provider_call_id="plan",
        tool="update_plan",
        tool_version="harnessix.update-plan/v1",
        effect_class=EffectClass.READ_ONLY,
        arguments={"steps": [{"step_id": "a", "description": value}]},
    )
    assert _decode_plan(call) is None


@pytest.mark.parametrize("byte_limit", [4096, 4097])
async def test_total_utf8_input_boundary_not_character_count(tmp_path, byte_limit):
    arguments = plan_arguments(
        PlanStep(step_id="a", description="".join(["界"] * 1000)),
        PlanStep(step_id="b", description="x"),
    )
    initial_size = len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    arguments["steps"][1]["description"] += "x" * (byte_limit - initial_size)
    encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert len(encoded) == byte_limit
    assert len(encoded.decode("utf-8")) < 4096
    _, _, turn = await run_script(tmp_path, [proposal_step(plan_call(arguments)), answer()])
    result = result_for(turn)
    assert turn.status is TurnStatus.COMPLETED
    assert len(plan_items(turn)) == int(byte_limit == 4096)
    assert result.outcome == ("succeeded" if byte_limit == 4096 else "failed")
    if byte_limit > 4096:
        assert result.error.code == "tool_invalid_arguments"


async def test_eight_steps_are_accepted_without_narrowing_native_plan_content(tmp_path):
    steps = tuple(PlanStep(step_id=str(index), description="检查") for index in range(8))
    _, _, turn = await run_script(
        tmp_path, [proposal_step(plan_call(plan_arguments(*steps))), answer()]
    )
    assert result_for(turn).output["step_count"] == 8
    assert plan_items(turn)[0].content.steps == steps
    assert (
        len(
            PlanContent(
                steps=(*steps, PlanStep(step_id="native-only", description="历史计划"))
            ).steps
        )
        == 9
    )


@pytest.mark.parametrize("max_chars", [223, 224])
async def test_output_bound_checks_full_result_before_any_plan_is_committed(tmp_path, max_chars):
    arguments = {"steps": [{"step_id": "a", "description": "a"}]}
    assert len(plan_call(arguments).model_dump_json()) < max_chars
    _, provider, turn = await run_script(
        tmp_path,
        [proposal_step(plan_call(arguments)), answer()],
        budget=Budget(max_output_chars=max_chars),
    )
    if max_chars == 223:
        assert turn.status is TurnStatus.FAILED and turn.error.code == "tool_output_too_large"
        assert not plan_items(turn)
        assert not any(
            isinstance(item.content, ToolResultContent) and item.content.outcome == "succeeded"
            for item in turn.items
        )
        assert len(provider.requests) == 1
    else:
        assert turn.status is TurnStatus.COMPLETED and len(plan_items(turn)) == 1
        assert len(result_for(turn).model_dump_json()) == max_chars


async def test_completed_plan_does_not_extend_original_max_steps(tmp_path):
    arguments = plan_arguments(
        PlanStep(step_id="test", description="模型自报已验证", status="completed")
    )
    _, provider, turn = await run_script(
        tmp_path, [proposal_step(plan_call(arguments)), answer()], budget=Budget(max_steps=1)
    )
    assert result_for(turn).outcome == "succeeded"
    assert plan_items(turn)[0].content.steps[0].status == "completed"
    assert turn.status is TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.model_steps == len(provider.requests) == 1
    assert turn.budget.max_steps == 1


async def test_planning_keeps_sampled_token_accounting_and_native_pre_dispatch_limit(tmp_path):
    start = attempt_start()
    usage = Usage(input_tokens=50_000, output_tokens=1)
    events = [
        start,
        ResponseStarted(response_id="response-1"),
        plan_call(),
        observed(
            start,
            completeness="complete",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        ResponseCompleted(finish_reason="tool_calls", usage=usage),
    ]
    _, provider, turn = await run_script(
        tmp_path, [events, answer()], budget=Budget(max_tokens=50_000, max_output_chars=4096)
    )
    assert turn.status is TurnStatus.FAILED and turn.error.code == "budget_exceeded"
    assert turn.usage == usage and turn.budget.max_tokens == 50_000
    assert len(provider.requests) == turn.model_steps == 1
    assert not plan_items(turn)
    assert not any(isinstance(item.content, ToolCallContent) for item in turn.items)
    assert all(attempt.status != "running" for attempt in turn.model_attempts)


async def test_successful_plan_keeps_usage_sum_and_request_budgets(tmp_path):
    first_usage = Usage(input_tokens=11, output_tokens=7)
    second_usage = Usage(input_tokens=13, output_tokens=5)
    final = answer()
    final[-1] = ResponseCompleted(usage=second_usage)
    budget = Budget(max_steps=2, max_tokens=50_000, max_output_chars=4096)
    _, provider, turn = await run_script(
        tmp_path, [proposal_step(plan_call(), usage=first_usage), final], budget=budget
    )
    assert turn.status is TurnStatus.COMPLETED and turn.usage == Usage(
        input_tokens=24, output_tokens=12
    )
    assert turn.budget == budget and all(request.budget == budget for request in provider.requests)
    assert turn.model_steps == len(provider.requests) == 2


async def test_all_completed_steps_do_not_guarantee_turn_success(tmp_path):
    arguments = plan_arguments(
        PlanStep(step_id="test", description="自报全部完成", status="completed")
    )
    _, _, turn = await run_script(
        tmp_path,
        [proposal_step(plan_call(arguments)), [ResponseFailed(code="transport", retryable=True)]],
    )
    assert plan_items(turn)[0].content.steps[0].status == "completed"
    assert result_for(turn).output["self_reported"] is True
    assert turn.status is TurnStatus.FAILED and turn.error.code == "provider_transport"


async def test_completed_steps_do_not_overwrite_failed_test_evidence(tmp_path):
    class FailedTestTools(RecordingTools):
        async def execute(self, call, cancel):
            cancel.checkpoint()
            self.calls.append(call)
            return ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                output={"exit_code": 1, "passed": False},
                error=AgentFailure(code="tool_failed", message="离线测试未通过"),
            )

    arguments = plan_arguments(
        PlanStep(step_id="test", description="自报测试通过", status="completed")
    )
    _, provider, turn = await run_script(
        tmp_path,
        [
            proposal_step(
                ToolCallCompleted(call_id="read", tool="test.read"), plan_call(arguments)
            ),
            answer(),
        ],
        tools=FailedTestTools(),
    )
    assert turn.status is TurnStatus.COMPLETED
    assert plan_items(turn)[0].content.steps[0].status == "completed"
    assert result_for(turn, "read").output == {"exit_code": 1, "passed": False}
    assert result_for(turn, "read").outcome == "failed"
    assert result_for(turn).output["self_reported"] is True
    assert any(
        isinstance(item.content, ToolResultContent)
        and item.content.output == {"exit_code": 1, "passed": False}
        for item in provider.requests[-1].history
    )


async def test_secret_in_valid_plan_is_rejected_before_publication(tmp_path):
    arguments = plan_arguments(PlanStep(step_id="a", description=CANARY))
    provider = ScriptedProvider([proposal_step(plan_call(arguments)), answer()])
    store = SQLiteSessionStore(tmp_path / "session.db")
    with protected() as scope:
        async with AgentRuntime(
            store, provider, enable_planning=True, public_output_protection=scope
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "检查计划公开保护", request_id="r")
    assert turn.status is TurnStatus.FAILED and turn.error.code == "public_output_secret_leak"
    assert not plan_items(turn)
    assert CANARY not in turn.model_dump_json()
    assert not any(CANARY.encode() in path.read_bytes() for path in tmp_path.glob("session.db*"))


@pytest.mark.parametrize("valid_input", [True, False], ids=["plan-events", "invalid-input-result"])
async def test_final_planning_batch_must_pass_protection_before_append(
    tmp_path, monkeypatch, valid_input
):
    arguments = plan_arguments() if valid_input else {"steps": []}
    rejected_kind = "plan" if valid_input else "tool_result"
    rejected_values = []

    def contains_kind(value):
        if isinstance(value, dict):
            return value.get("kind") == rejected_kind or any(
                contains_kind(child) for child in value.values()
            )
        return isinstance(value, list) and any(contains_kind(child) for child in value)

    class RejectFinalPlanningBatch:
        def __init__(self, delegate):
            self.delegate = delegate

        def assert_public_json(self, value, *, checkpoint):
            checkpoint()
            if contains_kind(value):
                rejected_values.append(value)
                raise KernelError("trusted_action_secret_leak", "DO_NOT_ECHO_BATCH_DIAGNOSTIC")
            self.delegate.assert_public_json(value, checkpoint=checkpoint)

        def assert_public_jsonl(self, body, *, checkpoint):
            self.delegate.assert_public_jsonl(body, checkpoint=checkpoint)

        def begin_public_text_step(self, *, checkpoint):
            return self.delegate.begin_public_text_step(checkpoint=checkpoint)

    store = SQLiteSessionStore(tmp_path / "session.db")
    append = AsyncMock(wraps=store.append)
    monkeypatch.setattr(store, "append", append)
    provider = ScriptedProvider([proposal_step(plan_call(arguments)), answer()])
    with protected() as scope:
        protection = RejectFinalPlanningBatch(scope)
        # 步骤输入本身允许；拒绝只能由最终Plan/失败结果的真实发布载荷触发。
        protection.assert_public_json(arguments, checkpoint=lambda: None)
        assert rejected_values == []
        async with AgentRuntime(
            store, provider, enable_planning=True, public_output_protection=protection
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "审计最终批次保护", request_id="r")

    call = next(item.content for item in turn.items if isinstance(item.content, ToolCallContent))
    # Runtime._finish可追加安全的配对失败事实；被拒绝的helper批次本身不得尝试append。
    attempted_publications = [
        draft
        for invocation in append.await_args_list
        for draft in invocation.args[1]
        if isinstance(draft.payload, ItemStarted | ItemFinished)
        and (
            isinstance(draft.payload.content, PlanContent)
            or isinstance(draft.payload.content, ToolResultContent)
            and draft.payload.content.call_id == call.call_id
            and (
                draft.payload.content.outcome == "succeeded"
                or draft.payload.content.error is not None
                and draft.payload.content.error.code == "tool_invalid_arguments"
            )
        )
    ]
    assert attempted_publications == [], "保护拒绝之前不得尝试append原始计划或helper结果"
    assert turn.status is TurnStatus.FAILED and turn.error.code == "public_output_secret_leak"
    assert not plan_items(turn)
    result = result_for(turn)
    assert result.outcome == "failed" and result.error == turn.error
    assert result.output is None
    assert len(rejected_values) == 1
    assert isinstance(rejected_values[0], list), "必须检查实际事件批次，而不是后续模型历史"
    assert len(provider.requests) == 1
    assert "DO_NOT_ECHO_BATCH_DIAGNOSTIC" not in turn.model_dump_json()
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_normalized_plan_body_obeys_output_bound_and_keeps_call_result_pair(tmp_path):
    arguments = {"steps": [{"step_id": str(index), "description": "x"} for index in range(8)]}
    call = plan_call(arguments)
    normalized_plan = PlanContent(
        steps=tuple(PlanStep.model_validate(step) for step in arguments["steps"])
    )
    assert all("status" not in step for step in arguments["steps"])
    assert len(call.model_dump_json()) < 400 < len(normalized_plan.model_dump_json())
    _, _, turn = await run_script(
        tmp_path, [proposal_step(call), answer()], budget=Budget(max_output_chars=400)
    )
    assert not plan_items(turn), "省略status的提案不能让规范化Plan正文绕过outputBound"
    result = result_for(turn)
    assert result.outcome == "failed" and result.error.code == "tool_output_too_large"
    assert result.output is None
    assert len(result.model_dump_json()) <= 400
