"""把产品投影转换为不含终端框架语义的有界展示行。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from harnessix.product_ui.projection import ProductViewState, ProjectedItem
from harnessix.product_ui.tool_summary import approval_text, tool_call_text, tool_result_text
from harnessix.protocol.contracts import (
    PublicApprovalRequestContent,
    PublicCompactionContent,
    PublicErrorContent,
    PublicItem,
    PublicPlanContent,
    PublicProcessStateContent,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolCallRejectionContent,
    PublicToolResultContent,
    ThreadView,
)

_REJECTION_REASONS = {
    "unregistered_tool": "工具未注册",
    "invalid_arguments_json": "工具参数不是合法JSON",
}
_PLAN_MARKS: Final = {"completed": "✓", "in_progress": "▸", "pending": "○"}
# 常见失败的白话说明与处理办法；其余失败显示原始说明并附错误码。
_FAILURE_TEXT: Final = {
    "provider_authentication": (
        "模型服务拒绝了 API Key，请检查密钥是否正确（harnessix code doctor --probe-model 可核对）"
    ),
    "provider_rate_limit": "模型服务限流，请稍后重试",
    "provider_transport": "连不上模型服务，请检查网络或代理设置",
    "provider_internal": "模型服务内部错误，请稍后重试",
    "provider_provider_internal": "模型服务内部错误，请稍后重试",
    "provider_context_overflow": "对话超出模型的上下文长度，请按 Ctrl+N 新建会话继续",
    "provider_max_output_tokens": "模型单次输出达到长度上限，可以让它分步完成",
    "budget_exceeded": "已达到本轮的 Token 上限，可以继续发送任务开始新一轮",
    "time_budget_exceeded": "已达到本轮的时间上限，可以继续发送任务开始新一轮",
    "uncertain_effect": (
        "操作结果无法确认，本轮已停止且不会自动重做；请先检查工作区改动（如 git status）再继续"
    ),
}
_TURN_STATUS_LABELS: Final = {
    "accepted": "准备中",
    "preparing_context": "准备中",
    "calling_model": "思考中",
    "executing_tools": "执行中",
    "waiting_approval": "等待你审批",
    "waiting_action": "等待操作完成",
    "waiting_input": "等待你回答",
    "finalizing": "收尾中",
    "cancelling": "正在取消",
    "completed": "已完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "interrupted": "已中断",
}


def turn_status_label(status: str) -> str:
    """Turn状态的中文说明；未知状态原样显示。"""

    return _TURN_STATUS_LABELS.get(status, status)


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    item_id: UUID
    role: str
    text: str
    transient: bool = False
    # 内容不会再变化；界面把开头连续的定稿行追加进历史区，其余行在下方实时区显示。
    final: bool = True


def _tool_call_line(
    entry: ProjectedItem,
    content: PublicToolCallContent,
    results: dict[UUID, ProjectedItem],
) -> tuple[TranscriptLine, ...]:
    """操作与其结果合成一行；并行调用时也能看出哪个结果属于哪个操作。"""

    text = tool_call_text(content)
    if text is None:
        return ()
    result = results.get(content.call_id)
    if result is None:
        return (TranscriptLine(entry.item.item_id, "操作", f"{text} …", final=False),)
    assert isinstance(result.item.content, PublicToolResultContent)
    return (
        TranscriptLine(
            entry.item.item_id,
            "操作",
            f"{text} → {tool_result_text(result.item.content)}",
            final=entry.final and result.final,
        ),
    )


def _entry_lines(
    entry: ProjectedItem, results: dict[UUID, ProjectedItem]
) -> tuple[TranscriptLine, ...]:
    content = entry.item.content
    if isinstance(content, PublicToolCallContent):
        return _tool_call_line(entry, content, results)
    if isinstance(content, PublicApprovalRequestContent):
        text = approval_text(content)
        if text is None:
            return ()
        decided = entry.final and content.decision is not None
        return (TranscriptLine(entry.item.item_id, "审批", text, final=decided),)
    return tuple(
        TranscriptLine(line.item_id, line.role, line.text, final=entry.final)
        for line in _item_lines(entry.item)
    )


def _item_lines(item: PublicItem) -> tuple[TranscriptLine, ...]:
    item_id = item.item_id
    content = item.content
    if isinstance(content, PublicTextContent):
        role = {
            "user_message": "你",
            "assistant_message": "Harnessix",
            "reasoning_summary": "推理摘要",
        }[content.kind]
        return (TranscriptLine(item_id, role, content.text),)
    if isinstance(content, PublicPlanContent):
        return tuple(
            TranscriptLine(
                item_id,
                "计划",
                f"{index}. {_PLAN_MARKS[step.status]} {step.description}",
            )
            for index, step in enumerate(content.steps, 1)
        )
    if isinstance(content, PublicToolCallRejectionContent):
        return (
            TranscriptLine(
                item_id,
                "工具调用拒绝",
                f"第 {content.model_step} 步：{_REJECTION_REASONS[content.reason]}，调用已拒绝",
            ),
        )
    if isinstance(content, PublicToolResultContent):
        # 对应的操作不在投影里时单独显示结果。
        return (TranscriptLine(item_id, "结果", tool_result_text(content)),)
    if isinstance(content, PublicQuestionRequestContent):
        options = "" if not content.options else " · 选项：" + " / ".join(content.options)
        return (TranscriptLine(item_id, "提问", content.question + options),)
    if isinstance(content, PublicQuestionAnswerContent):
        return (TranscriptLine(item_id, "回答", content.answer),)
    if isinstance(content, PublicProcessStateContent):
        return (TranscriptLine(item_id, "进程", f"{content.status} · {content.origin}"),)
    if isinstance(content, PublicErrorContent):
        if content.failure.code == "cancelled":
            # 用户主动取消不是错误。
            return (TranscriptLine(item_id, "提示", "任务已取消"),)
        failure = content.failure
        text = _FAILURE_TEXT.get(failure.code) or f"{failure.message}（{failure.code}）"
        return (TranscriptLine(item_id, "错误", text),)
    assert isinstance(content, PublicCompactionContent)
    return (
        TranscriptLine(
            item_id,
            "上下文",
            f"已压缩 {content.source_items} 项 · "
            f"{content.tokens_before}→{content.tokens_after} tokens",
        ),
    )


def transcript_lines(view: ProductViewState | None) -> tuple[TranscriptLine, ...]:
    """按持久Item顺序输出正文，再附加尚未持久完成的临时流。"""

    if view is None:
        return ()
    calls = {
        entry.item.content.call_id
        for entry in view.items
        if isinstance(entry.item.content, PublicToolCallContent)
    }
    results = {
        entry.item.content.call_id: entry
        for entry in view.items
        if isinstance(entry.item.content, PublicToolResultContent)
    }
    lines = [
        line
        for entry in view.items
        if not (
            isinstance(entry.item.content, PublicToolResultContent)
            and entry.item.content.call_id in calls
        )
        for line in _entry_lines(entry, results)
    ]
    final_ids = {entry.item.item_id for entry in view.items if entry.final}
    lines.extend(
        TranscriptLine(
            stream.item_id,
            "Harnessix（生成中）",
            stream.text if not stream.gap else "实时文本存在缺口，等待持久事件恢复",
            transient=True,
            final=False,
        )
        for stream in view.streams
        if stream.item_id not in final_ids
    )
    return tuple(lines)


def thread_label(view: ThreadView, status: str | None = None) -> str:
    """会话列表标签：创建时间、状态和轮数，不显示内部ID或Workspace路径。

    选中会话的状态由调用方传入当前Turn的实时状态，列表快照可能落后。
    """

    created = view.created_at.astimezone().strftime("%m-%d %H:%M")
    if status is None and view.latest_turn is not None:
        status = view.latest_turn.status
    label = "新会话" if status is None else turn_status_label(status)
    return f"{created} · {label} · {view.turn_count}轮"
