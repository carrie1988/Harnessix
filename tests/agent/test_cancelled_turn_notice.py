"""上一轮被用户取消后，新一轮补一条宿主说明：模型知道它已取消，界面不重复显示。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harnessix.agent.cancelled_turn_notice import (
    CANCELLED_TURN_NOTICE,
    cancelled_turn_notice_item_id,
)
from harnessix.agent.models import TextContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import FakeProvider
from harnessix.product_ui.projection import ProductViewState, ProjectedItem
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import PublicItem, PublicTextContent
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.test_runtime import WaitingProvider
from tests.product_ui.test_tui_usability import _thread


async def _cancelled_thread(store: SQLiteSessionStore, workspace: Path):
    provider = WaitingProvider()
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(workspace))
        task = asyncio.create_task(
            runtime.run_turn(thread.thread_id, "补全README并运行全部测试", request_id="big")
        )
        await asyncio.wait_for(provider.waiting.wait(), 3)
        active = (await store.get_thread(thread.thread_id)).turns[-1]
        await runtime.cancel(thread.thread_id, active.turn_id)
        cancelled = await asyncio.wait_for(task, 3)
    assert cancelled.status == TurnStatus.CANCELLED
    return thread, cancelled


def _texts(items) -> list[tuple[str, str]]:
    return [
        (item.content.kind, item.content.text)
        for item in items
        if isinstance(item.content, TextContent)
    ]


async def test_next_turn_after_a_cancel_tells_the_model(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    thread, _ = await _cancelled_thread(store, tmp_path)
    provider = FakeProvider("命令行选项是 data 与 --low")
    async with AgentRuntime(store, provider) as runtime:
        turn = await runtime.run_turn(thread.thread_id, "有哪些命令行选项？", request_id="next")

    assert turn.status is TurnStatus.COMPLETED
    assert _texts(turn.items)[:2] == [
        ("user_message", "有哪些命令行选项？"),
        ("user_message", CANCELLED_TURN_NOTICE),
    ]
    assert turn.items[1].item_id == cancelled_turn_notice_item_id(turn.turn_id)
    sent = _texts(provider.requests[0].history)
    assert sent[-2:] == [
        ("user_message", "有哪些命令行选项？"),
        ("user_message", CANCELLED_TURN_NOTICE),
    ]
    # 只说明紧接着的那一轮；再下一轮不再重复。
    async with AgentRuntime(store, FakeProvider()) as runtime:
        later = await runtime.run_turn(thread.thread_id, "谢谢", request_id="later")
    assert _texts(later.items)[:1] == [("user_message", "谢谢")]
    assert all(item.content != turn.items[1].content for item in later.items)


async def test_no_notice_after_a_completed_turn_or_on_retry(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "第一轮", request_id="one")
        second = await runtime.run_turn(thread.thread_id, "第二轮", request_id="two")
    assert [text for _, text in _texts(second.items)][:1] == ["第二轮"]
    assert CANCELLED_TURN_NOTICE not in [text for _, text in _texts(second.items)]

    thread, cancelled = await _cancelled_thread(SQLiteSessionStore(tmp_path / "s2.db"), tmp_path)
    async with AgentRuntime(SQLiteSessionStore(tmp_path / "s2.db"), FakeProvider()) as runtime:
        retried = await runtime.retry_turn(thread.thread_id, cancelled.turn_id, request_id="retry")
    assert CANCELLED_TURN_NOTICE not in [text for _, text in _texts(retried.items)]


def test_the_notice_is_not_shown_in_the_transcript() -> None:
    thread = _thread()
    turn_id = thread.thread_id

    def entry(item_id, text: str) -> ProjectedItem:
        return ProjectedItem(
            item=PublicItem(
                item_id=item_id,
                status="completed",
                content=PublicTextContent(kind="user_message", text=text),
            ),
            turn_id=turn_id,
            first_cursor=1,
            last_cursor=1,
            final=True,
        )

    from uuid import uuid4

    view = ProductViewState(
        thread=thread,
        durable_cursor=2,
        items=(
            entry(uuid4(), "有哪些命令行选项？"),
            entry(cancelled_turn_notice_item_id(turn_id), CANCELLED_TURN_NOTICE),
        ),
    )
    assert [line.text for line in transcript_lines(view)] == ["有哪些命令行选项？"]
