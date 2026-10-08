"""拒绝事实的正式展示、无操作边界及正常 Item 渲染回归。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from textual.app import App
from textual.widgets import Button, RichLog, Static

from harnessix.product_ui.controller import ControllerPhase, ProductControllerState
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interactions import pending_approval, pending_question
from harnessix.product_ui.main_view import ProductMainView
from harnessix.product_ui.projection import (
    ProductViewState,
    ProjectedItem,
    TransientItemStream,
    TurnProjection,
    apply_replay_page,
    cold_product_view,
)
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import (
    EventsReplayResult,
    ItemPublicEvent,
    PublicApprovalRequestContent,
    PublicCompactionContent,
    PublicErrorContent,
    PublicEvent,
    PublicFailure,
    PublicItem,
    PublicItemContent,
    PublicPlanContent,
    PublicPlanStep,
    PublicProcessStateContent,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolCallRejectionContent,
    PublicToolResultContent,
    ThreadView,
)


def view_for(*contents: PublicItemContent) -> ProductViewState:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    turn_id = uuid4()
    return ProductViewState(
        thread=ThreadView(
            thread_id=uuid4(),
            workspace="/workspace",
            cursor=len(contents),
            turn_count=1,
            created_at=now,
            updated_at=now,
        ),
        durable_cursor=len(contents),
        items=tuple(
            ProjectedItem(
                item=PublicItem(item_id=uuid4(), status="completed", content=content),
                turn_id=turn_id,
                first_cursor=index,
                last_cursor=index,
                final=True,
            )
            for index, content in enumerate(contents, 1)
        ),
        current_turn=TurnProjection(
            turn_id=turn_id,
            request_id="render-v2",
            status="calling_model",
            budget=None,
            usage=None,
            model_step=1,
        ),
    )


@pytest.mark.parametrize("model_step", [1, 1000])
def test_rejection_is_formal_chinese_text_without_approval_or_execution(model_step: int) -> None:
    view = view_for(PublicToolCallRejectionContent(call_id=uuid4(), model_step=model_step))
    lines = transcript_lines(view)
    assert len(lines) == 1
    assert lines[0].item_id == view.items[0].item.item_id
    assert lines[0].role == "工具调用拒绝"
    assert lines[0].text == f"第 {model_step} 步：工具未注册，调用已拒绝"
    assert lines[0].transient is False
    assert pending_approval(view) is None
    assert pending_question(view) is None
    assert not isinstance(view.items[0].item.content, PublicToolCallContent)


def test_rejection_never_satisfies_an_approval_tool_call_binding() -> None:
    call_id = uuid4()
    approval = PublicApprovalRequestContent(
        approval_type="tool",
        approval_id=uuid4(),
        call_id=call_id,
        request_fingerprint="a" * 64,
        policy_version="test-policy/v1",
    )
    view = view_for(PublicToolCallRejectionContent(call_id=call_id, model_step=1), approval)
    assert view.current_turn is not None
    waiting = replace(view, current_turn=replace(view.current_turn, status="waiting_approval"))
    with pytest.raises(ProductUIError) as caught:
        pending_approval(waiting)
    assert caught.value.code == "interaction_state_invalid"
    normal_call = PublicToolCallContent(
        call_id=call_id,
        tool="workspace.write",
        tool_version="1",
        effect_class="idempotent_write",
        requires_approval=True,
    )
    normal_item = PublicItem(
        item_id=waiting.items[0].item.item_id, status="completed", content=normal_call
    )
    normal = replace(
        waiting,
        items=(
            replace(waiting.items[0], item=normal_item),
            waiting.items[1],
        ),
    )
    assert pending_approval(normal) is not None


def test_v2_replay_renders_rejection_in_order_and_suppresses_final_item_stream() -> None:
    view = view_for(
        PublicToolCallRejectionContent(call_id=uuid4(), model_step=2),
        PublicTextContent(kind="assistant_message", text="任务已完成"),
    )
    replay = EventsReplayResult(
        thread_id=view.thread.thread_id,
        events=tuple(
            PublicEvent(
                event_id=uuid4(),
                thread_id=view.thread.thread_id,
                turn_id=entry.turn_id,
                cursor=index,
                occurred_at=view.thread.created_at,
                data=ItemPublicEvent(type="item_finished", item=entry.item),
            )
            for index, entry in enumerate(view.items, 1)
        ),
        scanned_through=2,
        has_more=False,
    )
    rebuilt = apply_replay_page(cold_product_view(view.thread), replay)
    rebuilt = replace(
        rebuilt,
        streams=(
            TransientItemStream(
                item_id=view.items[0].item.item_id,
                turn_id=view.items[0].turn_id,
                model_step=2,
                last_sequence=1,
                text="不应显示的临时文本",
            ),
        ),
    )
    assert [(line.role, line.text) for line in transcript_lines(rebuilt)] == [
        ("工具调用拒绝", "第 2 步：工具未注册，调用已拒绝"),
        ("Harnessix", "任务已完成"),
    ]


async def test_headless_main_view_displays_rejection_without_buttons_or_approval_prompt() -> None:
    view = view_for(PublicToolCallRejectionContent(call_id=uuid4(), model_step=3))
    state = ProductControllerState(
        phase=ControllerPhase.READY,
        threads=(view.thread,),
        selected_thread_id=view.thread.thread_id,
        thread_view=view,
        revision=1,
    )
    app = App()
    widget = ProductMainView()
    async with app.run_test(size=(100, 24)) as pilot:
        await app.mount(widget)
        assert await widget.render_state(state)
        await pilot.pause()
        rendered = "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)
        assert "工具调用拒绝：第 3 步：工具未注册，调用已拒绝" in rendered
        assert len(app.query(Button)) == 0
        status = str(app.query_one("#status", Static).render())
        assert "待审批" not in status
        assert "Ctrl+A" not in status


@pytest.mark.parametrize(
    ("content", "role", "text"),
    [
        (PublicTextContent(kind="user_message", text="检查项目"), "你", "检查项目"),
        (PublicTextContent(kind="assistant_message", text="完成"), "Harnessix", "完成"),
        (PublicTextContent(kind="reasoning_summary", text="摘要"), "推理摘要", "摘要"),
        (
            PublicToolCallContent(
                call_id=uuid4(),
                tool="workspace.read",
                tool_version="1",
                effect_class="read_only",
                requires_approval=False,
            ),
            "工具",
            "workspace.read@1 · read_only · 无需审批 · completed",
        ),
        (PublicToolResultContent(call_id=uuid4(), outcome="succeeded"), "工具", "结果 succeeded"),
        (
            PublicApprovalRequestContent(
                approval_type="tool",
                approval_id=uuid4(),
                call_id=uuid4(),
                request_fingerprint="b" * 64,
                policy_version="test/v1",
            ),
            "审批",
            "tool · test/v1 · 待决定",
        ),
        (
            PublicQuestionRequestContent(
                question_id=uuid4(),
                call_id=uuid4(),
                question="选择环境",
                options=("测试", "生产"),
            ),
            "提问",
            "选择环境 · 选项：测试 / 生产",
        ),
        (
            PublicQuestionAnswerContent(
                question_id=uuid4(),
                call_id=uuid4(),
                answer="测试",
            ),
            "回答",
            "测试",
        ),
        (
            PublicProcessStateContent(
                call_id=uuid4(),
                action_id=uuid4(),
                status="succeeded",
                origin="execution",
            ),
            "进程",
            "succeeded · execution",
        ),
        (
            PublicPlanContent(
                steps=(
                    PublicPlanStep(
                        step_id="inspect",
                        description="检查源码",
                        status="completed",
                    ),
                )
            ),
            "计划",
            "1. [completed] 检查源码",
        ),
        (
            PublicCompactionContent(
                source_items=2,
                tokens_before=20,
                tokens_after=10,
                tokenizer="test",
            ),
            "上下文",
            "已压缩 2 项 · 20→10 tokens",
        ),
        (
            PublicErrorContent(
                failure=PublicFailure(
                    code="file_missing",
                    message="文件不存在",
                    category="tool",
                )
            ),
            "错误",
            "file_missing：文件不存在",
        ),
    ],
)
def test_existing_content_rendering_is_unchanged(
    content: PublicItemContent,
    role: str,
    text: str,
) -> None:
    lines = transcript_lines(view_for(content))
    assert [(line.role, line.text) for line in lines] == [(role, text)]
