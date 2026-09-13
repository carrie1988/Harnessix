"""把产品投影转换为不含终端框架语义的有界展示行。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from harnessix.product_ui.projection import ProductViewState
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
    PublicToolResultContent,
    ThreadView,
)


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    item_id: UUID
    role: str
    text: str
    transient: bool = False


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
                f"{index}. [{step.status}] {step.description}",
            )
            for index, step in enumerate(content.steps, 1)
        )
    if isinstance(content, PublicToolCallContent):
        approval = "需要审批" if content.requires_approval else "无需审批"
        return (
            TranscriptLine(
                item_id,
                "工具",
                f"{content.tool}@{content.tool_version} · {content.effect_class} · "
                f"{approval} · {item.status}",
            ),
        )
    if isinstance(content, PublicToolResultContent):
        details = f"结果 {content.outcome}"
        if content.error is not None:
            details += f" · {content.error.code}：{content.error.message}"
        if content.diff_artifact is not None:
            details += " · 含完整Diff Artifact"
        return (TranscriptLine(item_id, "工具", details),)
    if isinstance(content, PublicApprovalRequestContent):
        decision = "待决定" if content.decision is None else content.decision.outcome
        return (
            TranscriptLine(
                item_id,
                "审批",
                f"{content.approval_type} · {content.policy_version} · {decision}",
            ),
        )
    if isinstance(content, PublicQuestionRequestContent):
        options = "" if not content.options else " · 选项：" + " / ".join(content.options)
        return (TranscriptLine(item_id, "提问", content.question + options),)
    if isinstance(content, PublicQuestionAnswerContent):
        return (TranscriptLine(item_id, "回答", content.answer),)
    if isinstance(content, PublicProcessStateContent):
        return (TranscriptLine(item_id, "进程", f"{content.status} · {content.origin}"),)
    if isinstance(content, PublicErrorContent):
        return (
            TranscriptLine(
                item_id,
                "错误",
                f"{content.failure.code}：{content.failure.message}",
            ),
        )
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
    lines = [line for entry in view.items for line in _item_lines(entry.item)]
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
