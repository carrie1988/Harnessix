"""仅从原生工具结算解释SWE-bench质量与状态一致性；不执行工具或修改评分。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    TurnStatusV18,
)
from harnessix.agent.tool_rejections import require_closed_rejection_items
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.evals.swebench.execution_contracts import SweBenchCaseExecutionState
from harnessix.tools.git_contracts import GitReviewOutput

_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_PATCH_TOOLS = frozenset({"apply_patch", WORKSPACE_PATCH_TOOL})
_GIT_REVIEW_TOOLS = frozenset({"git_status", "git_diff"})


@dataclass(frozen=True, slots=True)
class _ToolObservation:
    index: int
    call: ToolCallContent
    result: ToolResultContent

    @property
    def tool(self) -> str:
        return self.call.tool


def _stable_error_code(turn: Turn) -> str | None:
    if turn.status.value == "completed":
        return None
    code = turn.error.code if turn.error is not None else f"turn_{turn.status.value}"
    return code if _ERROR_CODE.fullmatch(code) else "turn_failed"


def _tool_observations(turn: Turn) -> tuple[_ToolObservation, ...]:
    require_closed_rejection_items(turn.items)
    calls: dict[UUID, ToolCallContent | ToolCallRejectionContent] = {}
    settled: set[UUID] = set()
    observations: list[_ToolObservation] = []
    for index, item in enumerate(turn.items):
        content = item.content
        if not isinstance(content, (ToolCallContent, ToolCallRejectionContent, ToolResultContent)):
            continue
        if item.status is not ItemStatus.COMPLETED or item.error is not None:
            raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具事实未完整结算")
        if isinstance(content, (ToolCallContent, ToolCallRejectionContent)):
            if content.call_id in calls:
                raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具调用身份重复")
            calls[content.call_id] = content
        else:
            call = calls.get(content.call_id)
            if call is None or content.call_id in settled:
                raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具结果配对无效")
            settled.add(content.call_id)
            if isinstance(call, ToolCallContent):
                observations.append(_ToolObservation(index, call, content))
    if calls.keys() != settled:
        raise KernelError("swebench_tool_state_unavailable", "SWE-bench工具调用缺少结算结果")
    return tuple(observations)


def _is_code_test_failure(observation: _ToolObservation) -> bool:
    return (
        observation.result.outcome == "failed"
        and observation.result.error is not None
        and observation.result.error.code == "process_nonzero_exit"
    )


def _turn_evidence_failure(turn: Turn) -> tuple[str, str] | None:
    """仅已计费且响应已知的模型契约失败可按原规则计入质量失败。"""
    if turn.status is not TurnStatus.COMPLETED:
        code = _stable_error_code(turn) or "swebench_turn_failed"
        # 仅映射已计费、响应身份已知的契约失败；原 Turn 和请求账本保持不变。
        if (
            turn.status is TurnStatus.FAILED
            and code in {"invalid_provider_output", "provider_invalid_provider_output"}
            and turn.accounted_attempts
            and all(
                attempt.response_id is not None and attempt.status in {"completed", "failed"}
                for attempt in turn.accounted_attempts
            )
        ):
            code = "swebench_model_response_invalid"
        return code, "SWE-bench Case Turn未成功完成"

    return None


def _review_evidence_failure(
    observations: tuple[_ToolObservation, ...], final_test_index: int
) -> tuple[str, str] | None:
    """只接受最新测试之后的完整Review；兼容原双工具结果而不降低门槛。"""
    reviews = tuple(
        item for item in observations if item.index > final_test_index and item.tool == "git_review"
    )
    if reviews:
        review = reviews[-1].result
        if review.outcome == "succeeded":
            try:
                output = GitReviewOutput.model_validate_json(json.dumps(review.output))
            except (ValueError, TypeError):
                pass
            else:
                if not output.status.truncated and not output.diff.truncated:
                    return None
        return "swebench_git_review_incomplete", "SWE-bench Git复核输出失败、无效或被截断"

    final_reviews: dict[str, _ToolObservation] = {}
    for item in observations:
        if item.index > final_test_index and item.tool in _GIT_REVIEW_TOOLS:
            final_reviews[item.tool] = item
    if set(final_reviews) != _GIT_REVIEW_TOOLS or any(
        item.result.outcome != "succeeded" for item in final_reviews.values()
    ):
        return "swebench_git_review_incomplete", "SWE-bench公开测试后缺少完整 Git 复核"
    return None


def _tool_evidence_failure(
    observations: tuple[_ToolObservation, ...], public_test_tool: str
) -> tuple[str, str] | None:
    """未知效果优先闭锁，Profile只允许成功或确定的代码非零退出。"""
    if any(
        item.tool != public_test_tool and item.result.outcome in {"unknown", "cancelled"}
        for item in observations
    ):
        return "swebench_tool_state_unavailable", "SWE-bench工具状态未知或已取消"
    if any(
        item.result.outcome != "succeeded" and not _is_code_test_failure(item)
        for item in observations
        if item.tool == public_test_tool
    ):
        return (
            "swebench_public_tests_unavailable",
            "SWE-bench公开测试发生非代码执行失败",
        )

    return None


def _case_evidence_failure(
    turn: Turn,
    patch: str,
    public_test_tool: str,
    *,
    cost_complete: bool,
    transport_failure_isolatable: bool = False,
) -> tuple[str, str] | None:
    try:
        observations = _tool_observations(turn)
    except KernelError:
        return "swebench_tool_state_unavailable", "SWE-bench工具事实未完整闭合"
    tool_failure = _tool_evidence_failure(observations, public_test_tool)
    if tool_failure is not None:
        return tool_failure
    public_tests = tuple(item for item in observations if item.tool == public_test_tool)

    if transport_failure_isolatable:
        return "swebench_provider_transport_isolated", "SWE-bench传输故障已冻结，失败不重试"
    if not cost_complete:
        return "swebench_cost_incomplete", "SWE-bench模型费用未完整结算"
    turn_failure = _turn_evidence_failure(turn)
    if turn_failure is not None:
        return turn_failure

    patch_results = tuple(item for item in observations if item.tool in _PATCH_TOOLS)
    if not patch_results:
        return "swebench_patch_missing", "SWE-bench Case未产生成功 Patch"
    final_patch = patch_results[-1]
    if final_patch.result.outcome != "succeeded":
        return "swebench_patch_failed", "SWE-bench Case最终 Patch未成功"
    if not patch.strip():
        return "swebench_patch_missing", "SWE-bench Case最终 Git Patch为空"

    final_public_tests = tuple(item for item in public_tests if item.index > final_patch.index)
    if not final_public_tests:
        return "swebench_public_tests_missing", "SWE-bench最终 Patch后缺少公开测试"
    if any(item.result.outcome != "succeeded" for item in final_public_tests):
        return "swebench_public_tests_failed", "SWE-bench最终 Patch后的公开测试未通过"
    final_test_index = final_public_tests[-1].index

    return _review_evidence_failure(observations, final_test_index)


def _require_case_evidence(
    state: SweBenchCaseExecutionState,
    thread_id: UUID,
    turn: Turn,
    patch: str,
) -> None:
    if (
        state.thread_id != thread_id
        or state.turn_id != turn.turn_id
        or state.turn_status != TurnStatusV18(turn.status)
        or state.input_tokens != turn.usage.input_tokens
        or state.output_tokens != turn.usage.output_tokens
        or state.patch_sha256 != hashlib.sha256(patch.encode("utf-8")).hexdigest()
    ):
        raise KernelError("swebench_case_evidence_mismatch", "SWE-bench Case证据与状态不一致")
