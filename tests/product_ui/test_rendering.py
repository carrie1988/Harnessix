from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from harnessix.product_ui.projection import (
    ProductViewState,
    ProjectedItem,
    TransientItemStream,
)
from harnessix.product_ui.rendering import thread_label, transcript_lines
from harnessix.protocol.contracts import (
    PublicFailure,
    PublicItem,
    PublicPlanContent,
    PublicPlanStep,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolResultContent,
    ThreadView,
)


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
    created = now.astimezone().strftime("%m-%d %H:%M")
    assert thread_label(thread) == f"{created} · 新会话 · 1轮"
    assert thread_label(thread, "waiting_approval") == f"{created} · 等待你审批 · 1轮"


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


def test_transcript_expands_plan_and_exposes_tool_lifecycle_and_stable_error() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    thread = ThreadView(
        thread_id=uuid4(),
        workspace="/workspace",
        cursor=3,
        turn_count=1,
        created_at=now,
        updated_at=now,
    )
    turn_id, call_id = uuid4(), uuid4()
    contents = (
        PublicPlanContent(
            steps=(
                PublicPlanStep(step_id="inspect", description="检查源码", status="completed"),
                PublicPlanStep(step_id="test", description="运行测试", status="in_progress"),
            )
        ),
        PublicToolCallContent(
            call_id=call_id,
            tool="workspace.read",
            tool_version="1",
            effect_class="read_only",
            requires_approval=False,
        ),
        PublicToolResultContent(
            call_id=call_id,
            outcome="failed",
            error=PublicFailure(
                code="file_missing",
                message="目标文件不存在",
                category="tool",
            ),
        ),
    )
    view = ProductViewState(
        thread=thread,
        durable_cursor=3,
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
    )

    lines = transcript_lines(view)

    assert [(line.role, line.text, line.final) for line in lines] == [
        ("计划", "1. ✓ 检查源码", True),
        ("计划", "2. ▸ 运行测试", True),
        # 结果并入对应操作行。
        ("操作", "调用 workspace.read → 失败：目标文件不存在（file_missing）", True),
    ]


def test_running_operations_and_streams_are_not_final() -> None:
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
    calls = [uuid4(), uuid4()]
    entries = [
        ProjectedItem(
            item=PublicItem(
                item_id=uuid4(),
                status="started",
                content=PublicToolCallContent(
                    call_id=call_id,
                    tool="read_file",
                    tool_version="1",
                    effect_class="read_only",
                    arguments={"path": name},
                    requires_approval=False,
                ),
            ),
            turn_id=turn_id,
            first_cursor=index,
            last_cursor=index,
            final=True,
        )
        for index, (call_id, name) in enumerate(zip(calls, ("a.py", "b.py"), strict=True), 1)
    ]
    # 两个并行读取中只有第二个先完成：各自显示在自己的操作行上。
    entries.append(
        ProjectedItem(
            item=PublicItem(
                item_id=uuid4(),
                status="completed",
                content=PublicToolResultContent(call_id=calls[1], outcome="succeeded"),
            ),
            turn_id=turn_id,
            first_cursor=3,
            last_cursor=3,
            final=True,
        )
    )
    view = ProductViewState(thread=thread, durable_cursor=3, items=tuple(entries))

    assert [(line.text, line.final) for line in transcript_lines(view)] == [
        ("读取 a.py …", False),
        ("读取 b.py → 完成", True),
    ]


def test_a_user_cancel_reads_as_a_notice_not_an_error() -> None:
    from harnessix.protocol.contracts import PublicErrorContent

    now = datetime(2026, 1, 1, tzinfo=UTC)
    thread = ThreadView(
        thread_id=uuid4(),
        workspace="/workspace",
        cursor=1,
        turn_count=1,
        created_at=now,
        updated_at=now,
    )

    def view(code: str, message: str) -> ProductViewState:
        failure = PublicFailure(code=code, message=message, category="cancelled")
        return ProductViewState(
            thread=thread,
            durable_cursor=1,
            items=(
                ProjectedItem(
                    item=PublicItem(
                        item_id=uuid4(),
                        status="completed",
                        content=PublicErrorContent(failure=failure),
                    ),
                    turn_id=uuid4(),
                    first_cursor=1,
                    last_cursor=1,
                    final=True,
                ),
            ),
        )

    assert [
        (line.role, line.text) for line in transcript_lines(view("cancelled", "取消已生效"))
    ] == [("提示", "任务已取消")]
    assert [
        (line.role, line.text) for line in transcript_lines(view("model_timeout", "模型超时"))
    ] == [("错误", "模型超时（model_timeout）")]
    wrong_key = transcript_lines(view("provider_authentication", "Provider 返回结构化失败"))
    assert wrong_key[0].text.startswith("模型服务拒绝了 API Key")
    unknown = transcript_lines(view("uncertain_effect", "存在未知效果，禁止自动重放"))
    assert unknown[0].role == "错误" and unknown[0].text.startswith("操作结果无法确认")
    assert "uncertain_effect" not in unknown[0].text
    # Beta反馈HX-05：连接失败只说检查网络，没有说怎样接着做。
    dropped = transcript_lines(view("provider_transport", "Provider 返回结构化失败"))
    assert dropped[0].text.startswith("连不上模型服务，请检查网络或代理设置")
    assert "直接发送“继续”就能接着做" in dropped[0].text
    # Beta Issue #3：上下文耗尽只显示内部说明和错误码，没有恢复办法。
    exhausted = transcript_lines(
        view("context_budget_exceeded", "Runtime 或用户必选指令超过可用 Context 输入预算")
    )
    assert exhausted[0].role == "错误" and "context_budget_exceeded" not in exhausted[0].text
    assert "Ctrl+N 新建会话" in exhausted[0].text
    assert "--context-window-tokens" in exhausted[0].text
    # Beta Issue #1：模型输出不合规时同样要说明怎样接着做。
    malformed = transcript_lines(
        view("provider_invalid_provider_output", "Provider 返回结构化失败")
    )
    assert malformed[0].text.startswith("模型服务返回的内容不完整或格式不对")
    assert "直接发送“继续”就能接着做" in malformed[0].text
