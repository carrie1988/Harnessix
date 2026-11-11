from __future__ import annotations

from datetime import timedelta
from itertools import product
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from harnessix.agent.completion_guard import (
    BASE_COMPLETION_NOTICES,
    CALL_AS_TEXT_NOTICE,
    CUT_OFF_REPLY_NOTICE,
    MAX_COMPLETION_NOTICES,
    NO_CHANGE_NOTICE,
    NOT_A_REPLY_NOTICE,
    UNFINISHED_STEP_NOTICE,
    completion_notice,
    completion_notice_item_id,
    last_tool_call_failed,
    notice_allowance,
    plan_unfinished,
    step_reply,
    workspace_patched,
)
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    Item,
    ItemContent,
    ItemStatus,
    PlanContent,
    PlanStep,
    TextContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.trusted_action_contracts import TrustedActionApprovalRequestContent
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step

SUMMARY_COPY = (
    '{"authority":"none","compaction_id":"01a1bfa5-6ac6-7bb3-ad29-411968a2ed04",'
    '"schema":"harnessix.compaction-summary/v1","summary":"## Goal","trust":"derived_history"}'
)
# 真实案例的通用形状（见tests/agent/test_reply_shape.py）：完整的键值记录、同一记录在收尾括号前
# 停下、带工具调用块的消息外壳、只包着文本的内容块外壳。
RECORD = (
    '{"c":100,"ent":"019f14a0-e174-7851-949f-a08389438c4d","l":{"detail":"mid_session",'
    '"session_len":173876,"turn_requests":1,"turn_tool_calls":1},"model":"other-model",'
    '"stm":"one_retry_recommended","svc":"responses_reconnect_preserved","trs":27,"was":true}'
)
CUT_OFF_RECORD = RECORD[:-1]
MESSAGE_WITH_CALL = (
    '{"id":"105f","type":"message","role":"assistant","content":[{"text":"Checking the float '
    'path.","type":"text"},{"input":{"cmd":"grep -n last tsdb/*.go"},"id":"toolu_01",'
    '"name":"exec","type":"tool_use"}],"stop_reason":"tool_use"}'
)
WRAPPED_SUMMARY = (
    '{"text":"Final state confirmed. The fix is in place and verified.","type":"text"}'
)
NON_ANSWERS = (SUMMARY_COPY, RECORD, CUT_OFF_RECORD, MESSAGE_WITH_CALL, "  ")

Outcome = Literal["succeeded", "failed", "cancelled", "unknown"]
Presentation = Literal["tool", "patch_batch", "process"]
StepStatus = Literal["pending", "in_progress", "completed"]


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


def asked(reason: str, current: Turn) -> str:
    """守卫补发的全文：原因加上宿主已知的剩余模型调用次数。"""
    left = current.budget.max_steps - current.model_steps
    return f"{reason} Model calls left in this task: at most {left}."


def after_notice(current: Turn, notice: str, *, steps: int = 1) -> Turn:
    """当前步骤收到补发后，再过steps个模型步骤的Turn。"""
    sent = item(
        TextContent(kind="user_message", text=notice),
        completion_notice_item_id(current.turn_id, current.model_steps),
    )
    return current.model_copy(
        update={"items": (*current.items, sent), "model_steps": current.model_steps + steps}
    )


def patch_items(
    *, presentation: Presentation = "patch_batch", outcome: Outcome = "succeeded"
) -> list[Item]:
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


def plan_item(*statuses: StepStatus) -> Item:
    steps = tuple(
        PlanStep(step_id=str(index), description=f"step {index}", status=status)
        for index, status in enumerate(statuses)
    )
    return item(PlanContent(steps=steps))


def tool_result(outcome: Outcome) -> Item:
    return item(ToolResultContent(call_id=uuid4(), outcome=outcome))


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


def test_plan_unfinished_reads_the_latest_plan_of_the_turn() -> None:
    assert not plan_unfinished([])
    assert plan_unfinished([plan_item("completed", "in_progress", "pending")])
    assert plan_unfinished([plan_item("completed", "pending")])
    assert not plan_unfinished([plan_item("in_progress"), plan_item("completed")])
    assert plan_unfinished([plan_item("completed"), plan_item("completed", "in_progress")])


def test_last_tool_call_failed_reads_the_latest_result() -> None:
    assert not last_tool_call_failed([])
    assert last_tool_call_failed([tool_result("succeeded"), tool_result("failed")])
    assert last_tool_call_failed([tool_result("unknown")])
    assert not last_tool_call_failed([tool_result("failed"), tool_result("succeeded")])


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        # 尚无成功补丁、计划有未完成步骤、最近一次工具失败，任一成立即放宽到硬上限。
        ([], MAX_COMPLETION_NOTICES),
        ([*patch_items(outcome="failed")], MAX_COMPLETION_NOTICES),
        ([*patch_items(), plan_item("completed", "in_progress")], MAX_COMPLETION_NOTICES),
        ([*patch_items(), tool_result("failed")], MAX_COMPLETION_NOTICES),
        ([*patch_items()], BASE_COMPLETION_NOTICES),
        ([*patch_items(), plan_item("completed", "completed")], BASE_COMPLETION_NOTICES),
        ([tool_result("failed"), *patch_items()], BASE_COMPLETION_NOTICES),
    ],
)
def test_notice_allowance_follows_observable_state(items: list[Item], expected: int) -> None:
    assert notice_allowance(items) == expected
    assert BASE_COMPLETION_NOTICES <= expected <= MAX_COMPLETION_NOTICES


def test_unfinished_replies_get_a_notice_and_finished_ones_complete() -> None:
    bare = turn()
    assert completion_notice(bare, 1, "The bug is confirmed.") == asked(NO_CHANGE_NOTICE, bare)
    assert completion_notice(bare, 1, SUMMARY_COPY) == asked(NOT_A_REPLY_NOTICE, bare)
    patched = turn(*patch_items())
    assert completion_notice(patched, 1, "Fixed and verified.") is None
    assert completion_notice(patched, 1, SUMMARY_COPY) == asked(NOT_A_REPLY_NOTICE, patched)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("  ", NOT_A_REPLY_NOTICE),
        (RECORD, NOT_A_REPLY_NOTICE),
        (CUT_OFF_RECORD, CUT_OFF_REPLY_NOTICE),
        ('<tool_call>\n{"name": "exec", "arguments": {"cmd": "make te', CUT_OFF_REPLY_NOTICE),
        (MESSAGE_WITH_CALL, CALL_AS_TEXT_NOTICE),
        (
            '<tool_call>\n{"name": "exec", "arguments": {"cmd": "ls"}}\n</tool_call>',
            CALL_AS_TEXT_NOTICE,
        ),
    ],
)
def test_a_non_answer_gets_the_notice_for_its_shape_even_after_a_patch(
    reply: str, reason: str
) -> None:
    for current in (turn(), turn(*patch_items())):
        assert completion_notice(current, 1, reply) == asked(reason, current)


def test_the_cut_off_notice_says_so_and_asks_for_a_smaller_call() -> None:
    assert "cut off" in CUT_OFF_REPLY_NOTICE
    assert "again" in CUT_OFF_REPLY_NOTICE and "smaller" in CUT_OFF_REPLY_NOTICE
    assert "as text" in CALL_AS_TEXT_NOTICE and "real tool call" in CALL_AS_TEXT_NOTICE


def test_a_text_only_envelope_is_judged_by_the_text_inside() -> None:
    # 壳内是真实的总结：已有补丁时照常完成，不再被当成JSON记录退回。
    assert completion_notice(turn(*patch_items()), 1, WRAPPED_SUMMARY) is None
    bare = turn()
    assert completion_notice(bare, 1, WRAPPED_SUMMARY) == asked(NO_CHANGE_NOTICE, bare)
    announced = '{"text":"Final verification, then I\'ll summarize.","type":"text"}'
    patched = turn(*patch_items())
    assert completion_notice(patched, 1, announced) == asked(UNFINISHED_STEP_NOTICE, patched)
    assert completion_notice(patched, 1, '{"text":" ","type":"text"}') == asked(
        NOT_A_REPLY_NOTICE, patched
    )


def test_an_announced_step_without_a_call_gets_a_notice_even_after_a_patch() -> None:
    patched = turn(*patch_items())
    assert completion_notice(patched, 1, "Next step: rerun the tests.") == asked(
        UNFINISHED_STEP_NOTICE, patched
    )
    bare = turn()
    assert completion_notice(bare, 1, "Let me look at the caller.") == asked(
        UNFINISHED_STEP_NOTICE, bare
    )
    # 意图在结尾句：真实案例里回复以对现状的陈述开头，最后一句才说还要做什么。
    closing = "All 11 failures are date-related. My next step is to confirm they are pre-existing."
    assert completion_notice(patched, 1, closing) == asked(UNFINISHED_STEP_NOTICE, patched)
    # 回应补发后的同类回复照常完成，不循环。
    answered = after_notice(patched, asked(UNFINISHED_STEP_NOTICE, patched))
    assert completion_notice(answered, 2, "Next step: none, the task is done.") is None


def test_a_plain_answer_to_a_notice_is_accepted() -> None:
    bare = turn()
    answered = after_notice(bare, asked(NO_CHANGE_NOTICE, bare))
    assert completion_notice(answered, 2, "Already fixed with sed; tests pass.") is None
    assert completion_notice(answered, 2, SUMMARY_COPY) == asked(NOT_A_REPLY_NOTICE, answered)


def test_notices_stop_at_the_budgets() -> None:
    exhausted = turn(model_steps=5, max_steps=5)
    assert completion_notice(exhausted, 5, SUMMARY_COPY) is None
    spent = turn().model_copy(update={"usage": Usage(input_tokens=100_000)})
    assert completion_notice(spent, 1, SUMMARY_COPY) is None
    last = turn(model_steps=4, max_steps=5)
    assert completion_notice(last, 4, SUMMARY_COPY) == asked(NOT_A_REPLY_NOTICE, last)
    assert asked(NOT_A_REPLY_NOTICE, last).endswith("Model calls left in this task: at most 1.")


def notices_until_completion(current: Turn, reply: str, *, steps_between: int) -> int:
    """模型每次都以同一条回复收尾（其间可夹着工具步骤）时，守卫补发的次数。"""
    for sent in range(MAX_COMPLETION_NOTICES + 1):
        notice = completion_notice(current, current.model_steps, reply)
        if notice is None:
            return sent
        current = after_notice(current, notice, steps=steps_between)
    raise AssertionError("守卫在硬上限之后仍在补发")


@pytest.mark.parametrize(
    ("patched", "open_plan", "failed_call", "steps_between"),
    list(product((False, True), (False, True), (False, True), (1, 2))),
)
def test_the_guard_always_terminates(
    patched: bool, open_plan: bool, failed_call: bool, steps_between: int
) -> None:
    items = [
        *(patch_items() if patched else ()),
        *([plan_item("completed", "in_progress")] if open_plan else ()),
        *([tool_result("failed")] if failed_call else ()),
    ]
    unfinished = not patched or open_plan or failed_call
    allowance = MAX_COMPLETION_NOTICES if unfinished else BASE_COMPLETION_NOTICES
    # 步骤预算远大于硬上限：终止靠的是补发次数有界，不是预算耗尽。
    start = turn(*items, max_steps=1000)
    for reply in NON_ANSWERS:
        # 结构上不是答复的回复每次都补发，直到用完当前状态允许的次数。
        assert notices_until_completion(start, reply, steps_between=steps_between) == allowance
    for reply in ("Let me look at the caller.", "The bug is confirmed."):
        # 靠措辞与状态推断的回复问一次即止；隔着工具步骤反复出现时同样受次数约束。
        sent = notices_until_completion(start, reply, steps_between=steps_between)
        assert sent <= allowance
        if steps_between == 1:
            assert sent <= 1


def test_the_hard_cap_holds_while_the_state_keeps_looking_unfinished() -> None:
    current = turn(max_steps=1000)
    for sent in range(MAX_COMPLETION_NOTICES):
        notice = completion_notice(current, current.model_steps, CUT_OFF_RECORD)
        assert notice == asked(CUT_OFF_REPLY_NOTICE, current), sent
        # 每次补发之后模型又调用了一次工具并失败：未完成迹象始终存在。
        current = after_notice(current, notice, steps=2)
        current = current.model_copy(update={"items": (*current.items, tool_result("failed"))})
    assert completion_notice(current, current.model_steps, CUT_OFF_RECORD) is None


def test_the_hard_cap_holds_when_the_state_flips_between_steps() -> None:
    """状态在“看似完成”与“未完成”之间来回变化时，补发会恢复，但总次数仍不超过硬上限。"""
    current = turn(*patch_items(), max_steps=1000)
    sent = 0
    for round_number in range(4 * MAX_COMPLETION_NOTICES):
        # 每轮先有一个工具步骤：偶数轮成功（看似完成），奇数轮失败（未完成）。
        outcome: Outcome = "failed" if round_number % 2 else "succeeded"
        current = current.model_copy(
            update={
                "items": (*current.items, tool_result(outcome)),
                "model_steps": current.model_steps + 1,
            }
        )
        notice = completion_notice(current, current.model_steps, CUT_OFF_RECORD)
        if notice is not None:
            sent += 1
            current = after_notice(current, notice)
    assert sent == MAX_COMPLETION_NOTICES


def test_cut_off_records_keep_getting_notices_while_nothing_has_changed() -> None:
    """真实案例：完整记录之后两次截断记录，其间有工具步骤，全程没有补丁，预算只用了三分之一。

    旧规则把截断记录当成真实答复、第三次因固定上限放行，Turn以空补丁结束。
    """
    current = turn(plan_item("completed", "in_progress", "pending"), model_steps=28)
    first = completion_notice(current, 28, RECORD)
    assert first == asked(NOT_A_REPLY_NOTICE, current)
    current = after_notice(current, first, steps=5)
    second = completion_notice(current, 33, CUT_OFF_RECORD)
    assert second == asked(CUT_OFF_REPLY_NOTICE, current)
    current = after_notice(current, second, steps=3)
    third = completion_notice(current, 36, CUT_OFF_RECORD)
    assert third == asked(CUT_OFF_REPLY_NOTICE, current)
    assert third.endswith("Model calls left in this task: at most 64.")


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
    # 剩余步数随提示进入下一次请求的历史：默认预算16步，第1步之后还剩15次。
    assert second == [
        ("user_message", "fix the bug"),
        ("assistant_message", "The bug is confirmed."),
        ("user_message", NO_CHANGE_NOTICE + " Model calls left in this task: at most 15."),
    ]
    notice_id = completion_notice_item_id(completed.turn_id, 1)
    assert any(entry.item_id == notice_id for entry in completed.items)
    events = await store.events(thread.thread_id)
    assert any(getattr(event.payload, "reason", None) == "steering" for event in events)
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_runtime_stops_asking_at_the_hard_cap(tmp_path: Path) -> None:
    # 脚本远长于硬上限：若守卫不终止，Provider会被请求到第20步。
    provider = ScriptedProvider([answer(CUT_OFF_RECORD)] * 20)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, completion_guard=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(
            thread.thread_id, "fix the bug", request_id="cap", budget=Budget(max_steps=100)
        )

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == MAX_COMPLETION_NOTICES + 1
    notices = [
        entry.content.text
        for entry in completed.items
        if isinstance(entry.content, TextContent) and entry.content.kind == "user_message"
    ][1:]
    assert len(notices) == MAX_COMPLETION_NOTICES
    assert all(notice.startswith(CUT_OFF_REPLY_NOTICE) for notice in notices)
    assert notices[0].endswith("at most 99.") and notices[-1].endswith("at most 94.")
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_runtime_keeps_a_degenerating_model_working(tmp_path: Path) -> None:
    """真实案例的整轮重放：三次结构化乱码之间夹着工具步骤，全程没有补丁。

    旧规则在第三次乱码处结束Turn（5次请求、空补丁）；现在每次都补发，模型得以继续到给出答复。
    """
    provider = ScriptedProvider(
        [
            answer(RECORD),
            tool_step("test.read"),
            answer(CUT_OFF_RECORD),
            tool_step("test.read"),
            answer(CUT_OFF_RECORD),
            tool_step("test.read"),
            answer("Nothing to change."),
            answer("Nothing to change."),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, RecordingTools(), completion_guard=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(
            thread.thread_id, "fix the bug", request_id="degenerate", budget=Budget(max_steps=100)
        )

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 8
    notices = [
        entry.content.text
        for entry in completed.items
        if isinstance(entry.content, TextContent) and entry.content.kind == "user_message"
    ][1:]
    assert [notice.split(" Model calls left")[0] for notice in notices] == [
        NOT_A_REPLY_NOTICE,
        CUT_OFF_REPLY_NOTICE,
        CUT_OFF_REPLY_NOTICE,
        NO_CHANGE_NOTICE,
    ]


async def test_runtime_without_the_guard_completes_on_any_reply(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer("The bug is confirmed.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="off")

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 1


async def test_runtime_does_not_notice_on_the_last_budgeted_step(tmp_path: Path) -> None:
    provider = ScriptedProvider([answer(SUMMARY_COPY)] * 3)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, completion_guard=True) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(
            thread.thread_id, "fix the bug", request_id="last", budget=Budget(max_steps=3)
        )

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 3
    notices = [
        entry.content.text
        for entry in completed.items
        if isinstance(entry.content, TextContent) and entry.content.kind == "user_message"
    ][1:]
    # 每条提示如实写出剩余次数；最后一个预算步骤不再补发。
    assert [notice.rsplit(" ", 1)[-1] for notice in notices] == ["2.", "1."]


async def test_runtime_rejects_a_non_boolean_switch(tmp_path: Path) -> None:
    with pytest.raises(KernelError) as error:
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "session.db"),
            ScriptedProvider([]),
            completion_guard=1,  # type: ignore[arg-type]
        )
    assert error.value.code == "invalid_runtime_configuration"
