from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import AgentFailure, FailureCategory, KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    Budget,
    CompactionContent,
    ErrorContent,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PlanContent,
    PlanStep,
    TextContent,
    ToolCallContent,
    ToolResultContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseFailed
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


async def prepared(store: SQLiteSessionStore, workspace: Path):
    async with AgentRuntime(
        store,
        ScriptedProvider([tool_step("test.read"), answer()]),
        RecordingTools(),
    ) as runtime:
        thread = await runtime.create_thread(str(workspace))
        await runtime.run_turn(thread.thread_id, "旧消息", request_id="old")
    thread = await store.get_thread(thread.thread_id)
    turn_id, item_id = new_id(), new_id()
    content = TextContent(kind="user_message", text="新消息")
    payloads = [
        TurnStarted(request_id="new", request_fingerprint="0" * 64, budget=Budget()),
        ItemStarted(item_id=item_id, content=content),
        ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
        TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
    ]
    return await store.append(
        thread.thread_id,
        [EventDraft(turn_id=turn_id, payload=p) for p in payloads],
        expected_sequence=thread.sequence,
    )


async def record(store, thread, content):
    item_id = new_id()
    updated = await store.append(
        thread.thread_id,
        [
            EventDraft(
                turn_id=thread.active_turn_id, payload=ItemStarted(item_id=item_id, content=content)
            ),
            EventDraft(
                turn_id=thread.active_turn_id,
                payload=ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
            ),
        ],
        expected_sequence=thread.sequence,
    )
    return updated, item_id


async def test_plan_revisions_are_immutable_and_replayable(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepared(store, tmp_path)
    first = PlanContent(steps=(PlanStep(step_id="one", description="阅读代码"),))
    thread, first_id = await record(store, thread, first)
    second = PlanContent(
        supersedes=first_id,
        steps=(
            PlanStep(step_id="one", description="阅读代码", status="completed"),
            PlanStep(step_id="two", description="检查测试", status="in_progress"),
        ),
    )
    thread, _ = await record(store, thread, second)
    with pytest.raises(KernelError, match="最新"):
        await record(store, thread, second)
    assert replay(await store.events(thread.thread_id)) == thread


async def test_plan_content_cannot_change_between_start_and_finish(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepared(store, tmp_path)
    content = PlanContent(steps=(PlanStep(step_id="one", description="原计划"),))
    item_id = new_id()
    thread = await store.append(
        thread.thread_id,
        [
            EventDraft(
                turn_id=thread.active_turn_id, payload=ItemStarted(item_id=item_id, content=content)
            )
        ],
        expected_sequence=thread.sequence,
    )
    with pytest.raises(KernelError, match="不可变"):
        await store.append(
            thread.thread_id,
            [
                EventDraft(
                    turn_id=thread.active_turn_id,
                    payload=ItemFinished(
                        item_id=item_id,
                        status=ItemStatus.COMPLETED,
                        content=PlanContent(
                            steps=(PlanStep(step_id="one", description="换成其他计划"),)
                        ),
                    ),
                )
            ],
            expected_sequence=thread.sequence,
        )
    async with AgentRuntime(store, FakeProvider()):
        recovered = (await store.get_thread(thread.thread_id)).turns[-1]
        assert recovered.status == TurnStatus.INTERRUPTED
        assert recovered.items[-1].content.failure == recovered.error
        assert recovered.error.category == FailureCategory.INTERRUPTED
        assert next(i for i in recovered.items if i.item_id == item_id).status == ItemStatus.FAILED


async def test_compaction_validates_old_sources_and_tool_pairing(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepared(store, tmp_path)
    old = thread.turns[0]
    tool_call = next(i for i in old.items if isinstance(i.content, ToolCallContent))
    tool_result = next(i for i in old.items if isinstance(i.content, ToolResultContent))
    for sources in [(tool_call.item_id,), (new_id(),), (thread.turns[-1].items[0].item_id,)]:
        with pytest.raises(KernelError):
            await record(
                store,
                thread,
                CompactionContent(
                    source_item_ids=sources,
                    summary="摘要",
                    tokens_before=100,
                    tokens_after=10,
                    tokenizer="fixture",
                ),
            )
        assert await store.get_thread(thread.thread_id) == thread
    content = CompactionContent(
        source_item_ids=(tool_call.item_id, tool_result.item_id),
        summary="工具读取完成",
        tokens_before=100,
        tokens_after=10,
        tokenizer="fixture",
    )
    updated, _ = await record(store, thread, content)
    assert updated.turns[0] == old
    assert replay(await store.events(thread.thread_id)) == updated


@pytest.mark.parametrize(
    "content",
    [
        lambda: PlanContent(steps=()),
        lambda: PlanContent(steps=(PlanStep(step_id="a", description="a"),) * 2),
        lambda: PlanContent(
            steps=tuple(
                PlanStep(step_id=str(i), description="a", status="in_progress") for i in range(2)
            )
        ),
        lambda: CompactionContent(
            source_item_ids=(new_id(),),
            summary="s",
            tokens_before=10,
            tokens_after=10,
            tokenizer="t",
        ),
    ],
)
def test_invalid_semantic_values(content) -> None:
    with pytest.raises(ValidationError):
        content()


@pytest.mark.parametrize("version", [1, 2])
def test_new_items_cannot_masquerade_as_old_events(version: int) -> None:
    for content in [
        PlanContent(steps=(PlanStep(step_id="a", description="a"),)),
        CompactionContent(
            source_item_ids=(new_id(),), summary="s", tokens_before=2, tokens_after=1, tokenizer="t"
        ),
        ErrorContent(failure=AgentFailure(code="cancelled", message="取消")),
    ]:
        with pytest.raises(ValidationError):
            EventDraft(
                schema_version=version, payload=ItemStarted(item_id=new_id(), content=content)
            )


async def test_structured_provider_failure_has_error_item_and_retryable_hint(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    provider = ScriptedProvider([[ResponseFailed(code="rate_limit", retryable=True)]])
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        assert turn.status == TurnStatus.FAILED and turn.error.retryable
        assert turn.error.category == FailureCategory.PROVIDER
        assert turn.items[-1].content == ErrorContent(failure=turn.error)
        assert len(provider.requests) == 1
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_terminal_requires_matching_error_fact(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepared(store, tmp_path)
    failure = AgentFailure(code="budget_exceeded", message="预算耗尽")
    payload = TurnStateChanged(status=TurnStatus.FAILED, error=failure)
    with pytest.raises(KernelError, match="Error Item"):
        await store.append(
            thread.thread_id,
            [EventDraft(turn_id=thread.active_turn_id, payload=payload)],
            expected_sequence=thread.sequence,
        )
    thread, _ = await record(store, thread, ErrorContent(failure=failure))
    complete = await store.append(
        thread.thread_id,
        [EventDraft(turn_id=thread.active_turn_id, payload=payload)],
        expected_sequence=thread.sequence,
    )
    assert complete.active_turn_id is None


async def test_error_control_item_is_not_sent_to_next_provider(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(
        store, ScriptedProvider([[ResponseFailed(code="authentication")]])
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "旧失败任务", request_id="old")
    provider = FakeProvider()
    async with AgentRuntime(store, provider) as runtime:
        await runtime.run_turn(thread.thread_id, "新任务", request_id="new")
    assert all(not isinstance(i.content, ErrorContent) for i in provider.requests[0].history)


@pytest.mark.parametrize("status", [ItemStatus.FAILED, ItemStatus.CANCELLED])
async def test_compaction_failed_or_cancelled_keeps_original_history(
    tmp_path: Path, status
) -> None:
    store = SQLiteSessionStore(tmp_path / "s.db")
    thread = await prepared(store, tmp_path)
    original = thread.turns[0]
    content = CompactionContent(
        source_item_ids=tuple(i.item_id for i in original.items),
        summary="摘要",
        tokens_before=20,
        tokens_after=2,
        tokenizer="fixture",
    )
    item_id = new_id()
    updated = await store.append(
        thread.thread_id,
        [
            EventDraft(
                turn_id=thread.active_turn_id, payload=ItemStarted(item_id=item_id, content=content)
            ),
            EventDraft(
                turn_id=thread.active_turn_id,
                payload=ItemFinished(item_id=item_id, content=content, status=status),
            ),
        ],
        expected_sequence=thread.sequence,
    )
    assert updated.turns[0] == original
    assert replay(await store.events(thread.thread_id)) == updated
