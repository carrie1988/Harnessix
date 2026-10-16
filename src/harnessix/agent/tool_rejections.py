"""目录拒绝的固定结果及闭合配对；只验证事实，不签发执行或重试权限。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal
from uuid import UUID

from harnessix.agent.errors import AgentFailure, FailureCategory, KernelError
from harnessix.agent.models import (
    Item,
    ItemStatus,
    Thread,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
)

RejectionReason = Literal["unregistered_tool", "invalid_arguments_json"]
# 每种拒绝原因唯一允许的固定结果；文本只陈述合同，不含调用名称或参数值。
_REJECTION_FAILURES: dict[str, tuple[str, str]] = {
    "unregistered_tool": ("unknown_tool", "工具未注册"),
    "invalid_arguments_json": (
        "tool_arguments_invalid_json",
        "工具参数不是合法JSON对象，调用未执行；请重发完整、正确转义的JSON参数",
    ),
}


def rejection_result(
    call_id: UUID, reason: RejectionReason = "unregistered_tool"
) -> ToolResultContent:
    """该原因唯一允许的拒绝结果；所有效果字段由原合同默认保持为空。"""
    code, message = _REJECTION_FAILURES[reason]
    return ToolResultContent(
        call_id=call_id,
        outcome="failed",
        error=AgentFailure(
            code=code,
            message=message,
            category=FailureCategory.TOOL,
            retryable=False,
        ),
    )


def require_rejection_result(
    content: ToolResultContent, reason: RejectionReason | None = None
) -> None:
    """给定原因时须逐字等于该原因的固定结果；未知配对时须属于固定结果集合。"""
    reasons: tuple[RejectionReason, ...] = (
        ("unregistered_tool", "invalid_arguments_json") if reason is None else (reason,)
    )
    if not any(content == rejection_result(content.call_id, item) for item in reasons):
        raise KernelError("invalid_event", "目录拒绝只能配对固定无效果失败结果")


def require_closed_rejection_items(items: Iterable[Item]) -> None:
    """仅完整提交、重放及快照消费时调用，允许Reducer的临时事件前缀。"""
    rejected: dict[UUID, RejectionReason] = {}
    settled: set[UUID] = set()
    ordinary: set[UUID] = set()
    result_ids: set[UUID] = set()
    for item in items:
        content = item.content
        if isinstance(content, ToolCallRejectionContent):
            if (
                content.call_id in rejected
                or content.call_id in ordinary
                or content.call_id in result_ids
                or item.status != ItemStatus.COMPLETED
                or item.error is not None
            ):
                raise KernelError("invalid_event", "目录拒绝事实必须唯一且完整")
            rejected[content.call_id] = content.reason
        elif isinstance(content, ToolCallContent):
            if content.call_id in rejected:
                raise KernelError("invalid_event", "拒绝事实不能复用普通工具调用身份")
            ordinary.add(content.call_id)
        elif isinstance(content, ToolResultContent):
            if content.call_id in rejected:
                if (
                    content.call_id in result_ids
                    or item.status != ItemStatus.COMPLETED
                    or item.error is not None
                ):
                    raise KernelError("invalid_event", "目录拒绝结果必须唯一且完整")
                require_rejection_result(content, rejected[content.call_id])
                settled.add(content.call_id)
            result_ids.add(content.call_id)
    if set(rejected) != settled:
        raise KernelError("invalid_event", "目录拒绝事实缺少配对结果")


def require_closed_rejections(thread: Thread) -> None:
    """拒绝不得跨Turn结算；继承历史保持闭合，不转换为待执行调用。"""
    if thread.fork_snapshot is not None:
        require_closed_rejection_items(thread.fork_snapshot.items)
    for turn in thread.turns:
        require_closed_rejection_items(turn.items)
