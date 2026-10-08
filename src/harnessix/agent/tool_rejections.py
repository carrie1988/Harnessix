"""目录拒绝的固定结果及闭合配对；只验证事实，不签发执行或重试权限。"""

from __future__ import annotations

from collections.abc import Iterable
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


def rejection_result(call_id: UUID) -> ToolResultContent:
    """唯一允许的拒绝结果；所有效果字段由原合同默认保持为空。"""
    return ToolResultContent(
        call_id=call_id,
        outcome="failed",
        error=AgentFailure(
            code="unknown_tool",
            message="工具未注册",
            category=FailureCategory.TOOL,
            retryable=False,
        ),
    )


def require_rejection_result(content: ToolResultContent) -> None:
    if content != rejection_result(content.call_id):
        raise KernelError("invalid_event", "目录拒绝只能配对固定无效果失败结果")


def require_closed_rejection_items(items: Iterable[Item]) -> None:
    """仅完整提交、重放及快照消费时调用，允许Reducer的临时事件前缀。"""
    rejected: set[UUID] = set()
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
            rejected.add(content.call_id)
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
                require_rejection_result(content)
                settled.add(content.call_id)
            result_ids.add(content.call_id)
    if rejected != settled:
        raise KernelError("invalid_event", "目录拒绝事实缺少配对结果")


def require_closed_rejections(thread: Thread) -> None:
    """拒绝不得跨Turn结算；继承历史保持闭合，不转换为待执行调用。"""
    if thread.fork_snapshot is not None:
        require_closed_rejection_items(thread.fork_snapshot.items)
    for turn in thread.turns:
        require_closed_rejection_items(turn.items)
