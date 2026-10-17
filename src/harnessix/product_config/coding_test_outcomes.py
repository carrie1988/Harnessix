"""测试结果判定投影：被测代码或测试自身挂起导致的Profile超时属于代码级测试失败，而非基础设施故障。"""

from __future__ import annotations

from uuid import UUID

from harnessix.agent.models import ToolCallContent, ToolResultContent, Turn

_PROFILE_PREFIX = "run_profile."
_CODE_TEST_FAILURE = "process_nonzero_exit"
_TEST_TIMEOUT = "process_timeout"


def with_test_timeouts_as_failures(turn: Turn) -> Turn:
    """仅供工作流与证据判定的投影：Profile超时按确定的非零退出处理；模型可见结果不变。"""
    profile_calls: set[UUID] = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
        and item.content.tool.startswith(_PROFILE_PREFIX)
    }
    items = []
    for item in turn.items:
        content = item.content
        if (
            isinstance(content, ToolResultContent)
            and content.call_id in profile_calls
            and content.outcome == "failed"
            and content.error is not None
            and content.error.code == _TEST_TIMEOUT
        ):
            error = content.error.model_copy(update={"code": _CODE_TEST_FAILURE})
            item = item.model_copy(update={"content": content.model_copy(update={"error": error})})
        items.append(item)
    return turn.model_copy(update={"items": tuple(items)})


_ARGUMENT_REJECTION = "tool_invalid_arguments"


def without_rejected_profile_arguments(turn: Turn) -> Turn:
    """仅供工作流与证据判定的投影：参数校验拒绝的Profile调用从未运行，连同结果移出视图。

    这类失败来自模型参数（如把pytest选项当作选择器），既非代码失败也非基础设施故障；
    模型可见结果不变，工作流按“尚未测试”继续，证据不把它记为公开测试不可用。
    """
    profile_calls: set[UUID] = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, ToolCallContent)
        and item.content.tool.startswith(_PROFILE_PREFIX)
    }
    rejected: set[UUID] = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
        and item.content.call_id in profile_calls
        and item.content.outcome == "failed"
        and item.content.error is not None
        and item.content.error.code == _ARGUMENT_REJECTION
    }
    if not rejected:
        return turn
    items = tuple(
        item
        for item in turn.items
        if not (
            isinstance(item.content, (ToolCallContent, ToolResultContent))
            and item.content.call_id in rejected
        )
    )
    return turn.model_copy(update={"items": items})
