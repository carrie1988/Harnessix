"""把产品投影转换为不含终端框架语义的有界展示行。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from harnessix.product_ui.projection import ProductViewState
from harnessix.protocol.contracts import (
    PublicApprovalRequestContent,
    PublicErrorContent,
    PublicPlanContent,
    PublicProcessStateContent,
    PublicQuestionAnswerContent,
    PublicQuestionRequestContent,
    PublicTextContent,
    PublicToolCallContent,
    PublicToolResultContent,
    ThreadView,
)


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    item_id: UUID
    role: str
    text: str
    transient: bool = False


def _item_line(item_id: UUID, content: object) -> TranscriptLine:
    if isinstance(content, PublicTextContent):
        role = {
            "user_message": "你",
            "assistant_message": "Harnessix",
            "reasoning_summary": "推理摘要",
        }[content.kind]
        return TranscriptLine(item_id, role, content.text)
    if isinstance(content, PublicPlanContent):
        return TranscriptLine(item_id, "计划", f"共{len(content.steps)}个步骤")
    if isinstance(content, PublicToolCallContent):
        return TranscriptLine(item_id, "工具", f"开始 {content.tool}")
    if isinstance(content, PublicToolResultContent):
        return TranscriptLine(item_id, "工具", f"结果 {content.outcome}")
    if isinstance(content, PublicApprovalRequestContent):
        return TranscriptLine(item_id, "审批", "需要确认操作")
    if isinstance(content, PublicQuestionRequestContent):
        return TranscriptLine(item_id, "提问", content.question)
    if isinstance(content, PublicQuestionAnswerContent):
        return TranscriptLine(item_id, "回答", content.answer)
    if isinstance(content, PublicProcessStateContent):
        return TranscriptLine(item_id, "进程", content.status)
    if isinstance(content, PublicErrorContent):
        return TranscriptLine(item_id, "错误", content.failure.message)
    return TranscriptLine(item_id, "系统", "上下文已压缩")


def transcript_lines(view: ProductViewState | None) -> tuple[TranscriptLine, ...]:
    """按持久Item顺序输出正文，再附加尚未持久完成的临时流。"""

    if view is None:
        return ()
    lines = [_item_line(entry.item.item_id, entry.item.content) for entry in view.items]
    final_ids = {entry.item.item_id for entry in view.items if entry.final}
    lines.extend(
        TranscriptLine(
            stream.item_id,
            "Harnessix（生成中）",
            stream.text if not stream.gap else "实时文本存在缺口，等待持久事件恢复",
            transient=True,
        )
        for stream in view.streams
        if stream.item_id not in final_ids
    )
    return tuple(lines)


def thread_label(view: ThreadView) -> str:
    """为Thread列表生成不包含Workspace路径的稳定短标签。"""
    status = view.latest_turn.status if view.latest_turn is not None else "空会话"
    return f"{str(view.thread_id)[:8]} · {status} · {view.turn_count}轮"
