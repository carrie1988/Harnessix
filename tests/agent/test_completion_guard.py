from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.completion_guard import (
    MAX_COMPLETION_NOTICES,
    NO_CHANGE_NOTICE,
    NOT_A_REPLY_NOTICE,
    UNFINISHED_STEP_NOTICE,
    announces_next_step,
    completion_notice,
    completion_notice_item_id,
    not_a_reply,
    step_reply,
    workspace_patched,
)
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Item,
    ItemContent,
    ItemStatus,
    TextContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.trusted_action_contracts import TrustedActionApprovalRequestContent
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer

SUMMARY_COPY = (
    '{"authority":"none","compaction_id":"01a1bfa5-6ac6-7bb3-ad29-411968a2ed04",'
    '"schema":"harnessix.compaction-summary/v1","summary":"## Goal","trust":"derived_history"}'
)


def item(content: ItemContent, item_id: UUID | None = None) -> Item:
    return Item(item_id=item_id or uuid4(), status=ItemStatus.COMPLETED, content=content)


def turn(*items: Item, model_steps: int = 1, max_steps: int = 100) -> Turn:
    return Turn(
        turn_id=uuid4(),
        request_id="completion-guard",
        request_fingerprint="0" * 64,
        status=TurnStatus.CALLING_MODEL,
        budget=Budget(max_steps=max_steps),
        items=(item(TextContent(kind="user_message", text="fix the bug")), *items),
        model_steps=model_steps,
        created_at=utc_now(),
    )


def patch_items(*, presentation: str = "patch_batch", outcome: str = "succeeded") -> list[Item]:
    approval = TrustedActionApprovalRequestContent(
        approval_id=uuid4(),
        call_id=uuid4(),
        presentation=presentation,
        plan_id=uuid4(),
        plan_fingerprint="1" * 64,
        execution_fingerprint="2" * 64,
        request_fingerprint="3" * 64,
        policy_id="test.policy",
        policy_version="1",
        diff_artifact=ArtifactRef(
            artifact_id=uuid4(),
            sha256="4" * 64,
            size_bytes=10,
            records=1,
            complete=True,
            expires_at=utc_now() + timedelta(days=1),
        )
        if presentation == "patch_batch"
        else None,
    )
    result = ToolResultContent(call_id=approval.call_id, outcome=outcome)
    return [item(approval), item(result)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("  \n", True),
        (SUMMARY_COPY, True),
        ('{"error":{"code":"provider_error","message":"Bad Request"}}', True),
        ("Summary of harnessix.compaction-summary/v1 records", True),
        ("The bug is confirmed.", False),
        ("Fixed `parse()`; tests pass.", False),
        ("{not json} is how the template marks a slot", False),
        ('["a", "list"]', False),
    ],
)
def test_not_a_reply(text: str, expected: bool) -> None:
    assert not_a_reply(text) is expected


def test_step_reply_reads_only_items_after_the_request_history() -> None:
    earlier = item(TextContent(kind="assistant_message", text="earlier"))
    covered = item(TextContent(kind="assistant_message", text="compacted away"))
    user = item(TextContent(kind="user_message", text="task"))
    reply = item(TextContent(kind="assistant_message", text="The bug is confirmed."))
    reasoning = item(TextContent(kind="reasoning_summary", text="thinking"))
    items = (user, covered, earlier, reasoning, reply)
    assert step_reply(items, {user.item_id, earlier.item_id}) == "The bug is confirmed."
    assert step_reply(items, set()) == "compacted awayearlierThe bug is confirmed."


def test_workspace_patched_counts_only_successful_patch_batches() -> None:
    assert workspace_patched(patch_items())
    assert not workspace_patched(patch_items(outcome="failed"))
    assert not workspace_patched(patch_items(presentation="process"))
    assert not workspace_patched([])


def test_unfinished_replies_get_a_notice_and_finished_ones_complete() -> None:
    bare = turn()
    assert completion_notice(bare, 1, "The bug is confirmed.") == NO_CHANGE_NOTICE
    assert completion_notice(bare, 1, SUMMARY_COPY) == NOT_A_REPLY_NOTICE
    patched = turn(*patch_items())
    assert completion_notice(patched, 1, "Fixed and verified.") is None
    assert completion_notice(patched, 1, SUMMARY_COPY) == NOT_A_REPLY_NOTICE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Next step: enlarge the basis and rerun the search.", True),
        ("**Next step:** run the tests", True),
        ("Let me run the full test file to confirm.", True),
        ("I'll check the other call sites", True),
        ("接下来运行相关测试文件确认。", True),
        ("Now I will verify the change by running:", True),
        ("Fixed the parser to keep the root key; tests pass.", False),
        ("The fix is complete and the reproduction now prints the expected value.", False),
        ("已修复，相关测试通过。", False),
    ],
)
def test_announces_next_step(text: str, expected: bool) -> None:
    assert announces_next_step(text) is expected


def test_an_announced_step_without_a_call_gets_a_notice_even_after_a_patch() -> None:
    patched = turn(*patch_items())
    assert completion_notice(patched, 1, "Next step: rerun the tests.") == UNFINISHED_STEP_NOTICE
    assert completion_notice(turn(), 1, "Let me look at the caller.") == UNFINISHED_STEP_NOTICE
    draft = turn(*patch_items(), model_steps=2)
    notice = item(
        TextContent(kind="user_message", text=UNFINISHED_STEP_NOTICE),
        completion_notice_item_id(draft.turn_id, 1),
    )
    answered = draft.model_copy(update={"items": (*draft.items, notice)})
    # 回应补发后的同类回复照常完成，不循环。
    assert completion_notice(answered, 2, "Next step: none, the task is done.") is None


def test_a_plain_answer_to_a_notice_is_accepted() -> None:
    draft = turn(model_steps=2)
    notice = item(
        TextContent(kind="user_message", text=NO_CHANGE_NOTICE),
        completion_notice_item_id(draft.turn_id, 1),
    )
    answered = draft.model_copy(update={"items": (*draft.items, notice)})
    assert completion_notice(answered, 2, "Already fixed with sed; tests pass.") is None
    assert completion_notice(answered, 2, SUMMARY_COPY) == NOT_A_REPLY_NOTICE


def test_notices_stop_at_the_cap_and_at_the_step_budget() -> None:
    draft = turn(model_steps=MAX_COMPLETION_NOTICES + 1)
    notices = tuple(
        item(
            TextContent(kind="user_message", text=NOT_A_REPLY_NOTICE),
            completion_notice_item_id(draft.turn_id, step),
        )
        for step in range(1, MAX_COMPLETION_NOTICES + 1)
    )
    capped = draft.model_copy(update={"items": (*draft.items, *notices)})
    assert completion_notice(capped, MAX_COMPLETION_NOTICES + 1, SUMMARY_COPY) is None
    exhausted = turn(model_steps=5, max_steps=5)
    assert completion_notice(exhausted, 5, SUMMARY_COPY) is None


async def test_runtime_sends_a_notice_and_completes_on_the_answer(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer("The bug is confirmed."), answer("Nothing to change.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, completion_guard=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="guard")

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 2
    second = [
        (entry.content.kind, entry.content.text)
        for entry in provider.requests[1].history
        if isinstance(entry.content, TextContent)
    ]
    assert second == [
        ("user_message", "fix the bug"),
        ("assistant_message", "The bug is confirmed."),
        ("user_message", NO_CHANGE_NOTICE),
    ]
    notice_id = completion_notice_item_id(completed.turn_id, 1)
    assert any(entry.item_id == notice_id for entry in completed.items)
    events = await store.events(thread.thread_id)
    assert any(getattr(event.payload, "reason", None) == "steering" for event in events)
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_runtime_caps_notices_for_repeated_summary_copies(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer(SUMMARY_COPY)] * (MAX_COMPLETION_NOTICES + 1))
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, completion_guard=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="cap")

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == MAX_COMPLETION_NOTICES + 1
    notices = [
        entry
        for entry in completed.items
        if isinstance(entry.content, TextContent) and entry.content.text == NOT_A_REPLY_NOTICE
    ]
    assert len(notices) == MAX_COMPLETION_NOTICES


async def test_runtime_without_the_guard_completes_on_any_reply(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer("The bug is confirmed.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="off")

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 1


async def test_runtime_does_not_notice_on_the_last_budgeted_step(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer(SUMMARY_COPY)])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, completion_guard=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(
            thread.thread_id, "fix the bug", request_id="last", budget=Budget(max_steps=1)
        )

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 1


async def test_runtime_rejects_a_non_boolean_switch(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as error:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"),
            ScriptedProvider([]),
            completion_guard=1,  # type: ignore[arg-type]
        )
    assert error.value.code == "invalid_runtime_configuration"
