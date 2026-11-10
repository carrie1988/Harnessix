"""复现门禁：首次修改源码前，须先有一次针对复现草稿的公开测试以代码级失败结束。

全部判定只依赖持久Turn事实（调用参数与结果），恢复重放得到相同结论。门禁有界：
复现草稿测试两次未失败（问题在本环境难以复现），或源码修改已被拒绝三次，即自动解锁。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.errors import AgentFailure, FailureCategory
from harnessix.agent.models import ToolCallContent, ToolResultContent, Turn
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.product_config.coding_scratch import (
    SCRATCH_DIRECTORY,
    SCRATCH_FILE_PREFIX,
    is_scratch_path,
)

REPRODUCTION_REQUIRED = "workspace_patch_reproduction_required"
_PATCH_TOOLS = frozenset({"apply_patch", WORKSPACE_PATCH_TOOL})
_PROFILE_PREFIX = "run_profile."
_REPRODUCTION_FAILURES = frozenset({"process_nonzero_exit", "process_timeout"})
_UNLOCK_AFTER_PASSING_RUNS = 2
_UNLOCK_AFTER_REJECTIONS = 3

_REPRODUCE_VERBATIM = (
    "copy the issue's own example code verbatim into test_harnessix_scratch.py in an existing "
    "test directory (adapt only imports and fixtures) and assert the exact output, value or "
    "error the issue says it should produce"
)
LOCKED_NOTICE = (
    "Source edits stay locked until a reproduction fails. First "
    + _REPRODUCE_VERBATIM
    + "; then run it with run_profile selecting only that file and confirm it fails on the "
    "unmodified code. Scratch files can be edited now."
)
RUN_REPRODUCTION_NOTICE = (
    "Run only your test_harnessix_scratch.py now and confirm it fails on the unmodified code; "
    "source edits unlock once it fails."
)
REPRODUCTION_PASSED_NOTICE = (
    "Your reproduction passed on the unmodified code, so it does not reproduce the issue. Rewrite "
    "it: " + _REPRODUCE_VERBATIM + ". Run it again; source edits unlock once it fails."
)
VERBATIM_SELF_CHECK = (
    " Run the issue's own example code verbatim in the scratch test and assert the output it "
    "states."
)
_REJECTION_MESSAGE = (
    "源码修改尚未解锁：需先让复现草稿测试在未修改的代码上失败。请"
    "把问题描述中的示例代码原样写入现有测试目录下的test_harnessix_scratch.py并断言问题所述的期望结果，"
    "用run_profile只选择该文件运行并确认失败；复现草稿文件现在即可修改。"
)


@dataclass(frozen=True, slots=True)
class ReproductionGateState:
    delivered: bool
    confirmed: bool
    passing_runs: int
    rejections: int
    latest_reproduction_passed: bool

    @property
    def locked(self) -> bool:
        return not (
            self.delivered
            or self.confirmed
            or self.passing_runs >= _UNLOCK_AFTER_PASSING_RUNS
            or self.rejections >= _UNLOCK_AFTER_REJECTIONS
        )


def selects_scratch(selector: str) -> bool:
    """路径或原生模块标签（如Django的app.test_harnessix_scratch）是否指向复现草稿。"""
    target = selector.split("::", 1)[0]
    if is_scratch_path(target):
        return True
    parts = re.split(r"[./\\]", target)
    return SCRATCH_DIRECTORY in parts or any(part.startswith(SCRATCH_FILE_PREFIX) for part in parts)


def patch_paths(call: ToolCallContent) -> tuple[str, ...]:
    """Patch调用请求修改的全部路径；参数形态不可识别时返回空元组。"""
    arguments = call.arguments
    if call.tool == "apply_patch":
        path = arguments.get("path")
        return (path,) if isinstance(path, str) else ()
    files = arguments.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except ValueError:
            return ()
    if not isinstance(files, list):
        return ()
    return tuple(
        path
        for item in files
        if isinstance(item, dict) and isinstance(path := item.get("path"), str)
    )


def _touches_source(call: ToolCallContent) -> bool:
    return call.tool in _PATCH_TOOLS and any(
        not is_scratch_path(path) for path in patch_paths(call)
    )


def _is_reproduction_run(call: ToolCallContent) -> bool:
    selectors = call.arguments.get("selectors")
    return (
        call.tool.startswith(_PROFILE_PREFIX)
        and isinstance(selectors, list)
        and any(isinstance(item, str) and selects_scratch(item) for item in selectors)
    )


def reproduction_gate_state(turn: Turn) -> ReproductionGateState:
    calls: dict[UUID, ToolCallContent] = {}
    delivered = confirmed = latest_passed = False
    passing = rejections = 0
    for item in turn.items:
        content = item.content
        if isinstance(content, ToolCallContent):
            calls[content.call_id] = content
            continue
        if not isinstance(content, ToolResultContent) or content.call_id not in calls:
            continue
        call = calls[content.call_id]
        code = content.error.code if content.error is not None else None
        if call.tool in _PATCH_TOOLS:
            if code == REPRODUCTION_REQUIRED:
                rejections += 1
            elif content.outcome == "succeeded" and _touches_source(call):
                delivered = True
        elif _is_reproduction_run(call):
            if content.outcome == "succeeded":
                passing += 1
                latest_passed = True
            elif code in _REPRODUCTION_FAILURES:
                confirmed = True
                latest_passed = False
    return ReproductionGateState(delivered, confirmed, passing, rejections, latest_passed)


def reproduction_admission_failure(turn: Turn, call: ToolCallContent) -> AgentFailure | None:
    """门禁未解锁时拒绝触及源码的Patch；只改复现草稿的Patch始终放行。"""
    if not _touches_source(call) or not reproduction_gate_state(turn).locked:
        return None
    return AgentFailure(
        code=REPRODUCTION_REQUIRED,
        message=_REJECTION_MESSAGE,
        category=FailureCategory.TOOL,
        retryable=False,
    )


def without_reproduction_rejections(turn: Turn) -> Turn:
    """仅供工作流与证据判定的投影：门禁拒绝的Patch从未准备或执行，连同结果移出视图。"""
    rejected = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, ToolResultContent)
        and item.content.error is not None
        and item.content.error.code == REPRODUCTION_REQUIRED
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
