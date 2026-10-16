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
