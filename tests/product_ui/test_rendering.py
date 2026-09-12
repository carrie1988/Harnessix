from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from harnessix.product_ui.projection import (
    ProductViewState,
    ProjectedItem,
    TransientItemStream,
)
from harnessix.product_ui.rendering import thread_label, transcript_lines
from harnessix.protocol.contracts import PublicItem, PublicTextContent, ThreadView


def test_transcript_lines_keep_persisted_order_and_append_transient_text() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    thread = ThreadView(
        thread_id=uuid4(),
        workspace="/workspace",
        cursor=2,
        turn_count=1,
        created_at=now,
        updated_at=now,
    )
    turn_id = uuid4()
    user_id = uuid4()
    assistant_id = uuid4()
    view = ProductViewState(
        thread=thread,
        durable_cursor=2,
        items=(
            ProjectedItem(
                item=PublicItem(
                    item_id=user_id,
                    status="completed",
                    content=PublicTextContent(kind="user_message", text="检查项目"),
                ),
                turn_id=turn_id,
                first_cursor=1,
                last_cursor=1,
                final=True,
            ),
        ),
        streams=(
            TransientItemStream(
                item_id=assistant_id,
                turn_id=turn_id,
                model_step=1,
                last_sequence=1,
                text="正在检查",
            ),
        ),
    )

    lines = transcript_lines(view)

    assert [(line.role, line.text, line.transient) for line in lines] == [
        ("你", "检查项目", False),
        ("Harnessix（生成中）", "正在检查", True),
    ]
    assert thread_label(thread).startswith(f"{str(thread.thread_id)[:8]} · 空会话")


def test_transcript_gap_is_explicit_and_final_item_suppresses_stream() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    thread = ThreadView(
        thread_id=uuid4(),
        workspace="/workspace",
        cursor=2,
        turn_count=1,
        created_at=now,
        updated_at=now,
    )
    turn_id = uuid4()
    item_id = uuid4()
    final = ProductViewState(
        thread=thread,
        durable_cursor=2,
        items=(
            ProjectedItem(
                item=PublicItem(
                    item_id=item_id,
                    status="completed",
                    content=PublicTextContent(kind="assistant_message", text="最终正文"),
                ),
                turn_id=turn_id,
                first_cursor=1,
                last_cursor=2,
                final=True,
            ),
        ),
        streams=(
            TransientItemStream(
                item_id=item_id,
                turn_id=turn_id,
                model_step=1,
                last_sequence=0,
                text="",
                gap=True,
            ),
        ),
    )

    assert [(line.role, line.text) for line in transcript_lines(final)] == [
        ("Harnessix", "最终正文")
    ]

    missing = final.__class__(
        thread=thread,
        durable_cursor=1,
        streams=final.streams,
    )
    assert transcript_lines(missing)[0].text == "实时文本存在缺口，等待持久事件恢复"
