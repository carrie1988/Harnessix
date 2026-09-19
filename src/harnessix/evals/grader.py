"""不依赖 Golden Patch 的 Coding Eval v1 确定性评分器。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.models import (
    ItemStatus,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.evals.contracts import (
    CodingEvalEnvironment,
    CodingEvalReport,
    CodingEvalTask,
    EvalCheck,
    EvalCheckCode,
    EvalFailureCategory,
    EvalFinalAnswer,
    EvalFinalAnswerEvidence,
    EvalGitEvidence,
    EvalMetrics,
    EvalOutcome,
    EvalTestObservation,
    elapsed_seconds,
)


@dataclass(frozen=True)
class _TestResult:
    position: int
    profile: str
    passed: bool


@dataclass(frozen=True)
class _Transcript:
    tests: tuple[_TestResult, ...]
    patch_positions: tuple[int, ...]
    git_status_positions: tuple[int, ...]
    git_diff_positions: tuple[int, ...]
    final_position: int | None
    final_text: str | None
    final_answer: EvalFinalAnswer | None
    tool_calls: int
    approvals: int


def _final_answer(text: str) -> EvalFinalAnswer | None:
    try:
        value = json.loads(text)
        if not isinstance(value, dict):
            return None
        return EvalFinalAnswer.model_validate_json(text, strict=True)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
        return None


def _is_applied_patch(result: ToolResultContent) -> bool:
    if result.outcome != "succeeded":
        return False
    if result.trusted_action is not None:
        return result.trusted_action.state == "succeeded"
    if result.patch is not None:
        return result.patch.state == "applied"
    if result.patch_batch is not None and result.patch_batch.execution is not None:
        return result.patch_batch.execution.effect == "applied"
    return False


def _test_result(
    output: Any,
    position: int,
    *,
    expected_profile: str | None = None,
) -> _TestResult | None:
    if not isinstance(output, dict):
        return None
    profile = output.get("profile")
    if not isinstance(profile, str) or (
        expected_profile is not None and profile != expected_profile
    ):
        return None
    passed = output.get("passed")
    if type(passed) is not bool:
        state = output.get("state")
        stop_reason = output.get("stop_reason")
        returncode = output.get("returncode")
        if state != "exited" or stop_reason != "exited" or type(returncode) is not int:
            return None
        passed = returncode == 0
    return _TestResult(position=position, profile=profile, passed=passed)


def _record_tool_result(
    call: ToolCallContent,
    result: ToolResultContent,
    position: int,
    tests: list[_TestResult],
    patches: list[int],
    statuses: list[int],
    diffs: list[int],
) -> None:
    if call.tool == "run_tests":
        if result.outcome != "succeeded":
            return
        observed = _test_result(result.output, position)
        if observed is not None:
            tests.append(observed)
    elif call.tool.startswith("run_profile."):
        profile = call.tool.removeprefix("run_profile.")
        observed = _test_result(result.output, position, expected_profile=profile)
        if observed is not None:
            tests.append(observed)
    elif call.tool in {"apply_patch", "apply_patch_batch"} and _is_applied_patch(result):
        patches.append(position)
    elif call.tool == "git_status" and result.outcome == "succeeded":
        statuses.append(position)
    elif call.tool == "git_diff" and result.outcome == "succeeded":
        diffs.append(position)


def _transcript(turn: Turn) -> _Transcript:
    calls: dict[UUID, ToolCallContent] = {}
    tests: list[_TestResult] = []
    patches: list[int] = []
    statuses: list[int] = []
    diffs: list[int] = []
    final_position: int | None = None
    final_text: str | None = None
    final_answer: EvalFinalAnswer | None = None
    approvals = 0
    for position, item in enumerate(turn.items):
        if item.status != ItemStatus.COMPLETED:
            continue
        content = item.content
        if isinstance(content, ToolCallContent):
            calls[content.call_id] = content
            continue
        if isinstance(
            content,
            (
                PatchApprovalRequestContent,
                PatchBatchApprovalRequestContent,
                ProcessApprovalRequestContent,
                TrustedActionApprovalRequestContent,
            ),
        ):
            if content.decision is not None and content.decision.outcome == "approved":
                approvals += 1
            continue
        if isinstance(content, ToolResultContent):
            call = calls.get(content.call_id)
            if call is not None:
                _record_tool_result(call, content, position, tests, patches, statuses, diffs)
            continue
        if isinstance(content, TextContent) and content.kind == "assistant_message":
            final_position = position
            final_text = content.text
            final_answer = _final_answer(content.text)
    return _Transcript(
        tests=tuple(tests),
        patch_positions=tuple(patches),
        git_status_positions=tuple(statuses),
        git_diff_positions=tuple(diffs),
        final_position=final_position,
        final_text=final_text,
        final_answer=final_answer,
        tool_calls=len(calls),
        approvals=approvals,
    )


def _observation_set(
    observations: tuple[EvalTestObservation, ...],
    expected: tuple[str, ...],
    *,
    phase: str,
    passed: bool,
) -> bool:
    names = [observation.check_id for observation in observations]
    return (
        names == sorted(set(names))
        and tuple(names) == expected
        and all(
            observation.phase == phase and observation.passed is passed
            for observation in observations
        )
    )


def _selected_observations(
    observations: tuple[EvalTestObservation, ...], names: tuple[str, ...]
) -> tuple[EvalTestObservation, ...]:
    selected = tuple(observation for observation in observations if observation.check_id in names)
    return tuple(sorted(selected, key=lambda observation: observation.check_id))


def _feedback_order(task: CodingEvalTask, transcript: _Transcript) -> bool:
    if not transcript.patch_positions:
        return False
    patch = transcript.patch_positions[0]
    for profile in task.required_test_profiles:
        profile_tests = [test for test in transcript.tests if test.profile == profile]
        if not any(not test.passed and test.position < patch for test in profile_tests):
            return False
        if not any(test.passed and test.position > patch for test in profile_tests):
            return False
        if not profile_tests[-1].passed:
            return False
    return True


def _git_feedback_order(task: CodingEvalTask, transcript: _Transcript) -> bool:
    passed = [
        test.position
        for test in transcript.tests
        if test.profile in task.required_test_profiles and test.passed
    ]
    if not passed or not transcript.git_status_positions or not transcript.git_diff_positions:
        return False
    last_pass = max(passed)
    status = next(
        (position for position in transcript.git_status_positions if position > last_pass), None
    )
    diff = next(
        (
            position
            for position in transcript.git_diff_positions
            if status is not None and position > status
        ),
        None,
    )
    return (
        diff is not None
        and transcript.final_position is not None
        and transcript.final_position > diff
    )


def _contains_finding(summary: str, finding_id: str) -> bool:
    """只接受独立Finding ID，避免前后缀字符串误命中。"""

    boundary = r"[a-z0-9_-]"
    return (
        re.search(
            rf"(?<!{boundary}){re.escape(finding_id)}(?!{boundary})",
            summary,
        )
        is not None
    )


def _answer_consistent(
    task: CodingEvalTask,
    transcript: _Transcript,
    git: EvalGitEvidence,
    required_review_finding_ids: tuple[str, ...],
) -> bool:
    answer = transcript.final_answer
    if answer is None or answer.changed_paths != git.changed_paths:
        return False
    tests = {test.profile: test.passed for test in answer.tests}
    return (
        tuple(tests) == task.required_test_profiles
        and all(tests.values())
        and all(
            _contains_finding(answer.summary, finding_id)
            for finding_id in required_review_finding_ids
        )
    )


def _answer_evidence(transcript: _Transcript) -> EvalFinalAnswerEvidence | None:
    if transcript.final_text is None:
        return None
    body = transcript.final_text.encode("utf-8")
    answer = transcript.final_answer
    return EvalFinalAnswerEvidence(
        parsed=answer is not None,
        response_sha256=hashlib.sha256(body).hexdigest(),
        response_utf8_bytes=len(body),
        changed_paths=answer.changed_paths if answer is not None else (),
        tests=answer.tests if answer is not None else (),
    )


def _check(
    code: EvalCheckCode,
    passed: bool,
    category: EvalFailureCategory,
    success: str,
    failure: str,
) -> EvalCheck:
    return EvalCheck(
        code=code,
        passed=passed,
        category=category,
        message=success if passed else failure,
    )


def _review_finding_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    if tuple(sorted(set(value))) != value:
        raise ValueError("Review Finding ID必须唯一并排序")
    return value


def _eval_outcome(
    checks: tuple[EvalCheck, ...], repository_matched: bool
) -> tuple[tuple[EvalFailureCategory, ...], EvalOutcome]:
    categories = tuple(sorted({check.category for check in checks if not check.passed}))
    outcome: EvalOutcome = (
        "passed"
        if not categories
        else "invalid"
        if not repository_matched or "eval_infrastructure" in categories
        else "failed"
    )
    return categories, outcome


def grade_coding_eval(
    task: CodingEvalTask,
    turn: Turn,
    *,
    run_id: UUID,
    environment: CodingEvalEnvironment,
    started_at: datetime,
    completed_at: datetime,
    baseline_observations: tuple[EvalTestObservation, ...],
    final_observations: tuple[EvalTestObservation, ...],
    git: EvalGitEvidence,
    required_review_finding_ids: tuple[str, ...] = (),
) -> CodingEvalReport:
    """按行为、回归、Git 与真实 Session 事实评分，不比较唯一补丁文本。"""

    required_review_finding_ids = _review_finding_ids(required_review_finding_ids)
    baseline = tuple(sorted(baseline_observations, key=lambda item: item.check_id))
    final = tuple(sorted(final_observations, key=lambda item: item.check_id))
    behavior = _selected_observations(final, task.behavior_checks)
    regressions = _selected_observations(final, task.regression_checks)
    transcript = _transcript(turn)
    changed = set(git.changed_paths)
    allowed = set(task.allowed_changed_paths)
    repository_matched = git.baseline_tree_sha256 == task.repository.baseline_tree_sha256
    checks = (
        _check(
            "task_repository_matched",
            repository_matched,
            "eval_infrastructure",
            "任务基线树与运行证据一致",
            "任务基线树与运行证据不一致",
        ),
        _check(
            "baseline_checks_failed",
            _observation_set(baseline, task.baseline_checks, phase="baseline", passed=False),
            "eval_infrastructure",
            "缺陷基线按预期失败",
            "缺陷基线缺失、重复或意外通过",
        ),
        _check(
            "final_check_set_matched",
            tuple(observation.check_id for observation in final)
            == tuple(sorted((*task.behavior_checks, *task.regression_checks)))
            and all(observation.phase == "final" for observation in final),
            "eval_infrastructure",
            "最终检查集合与任务定义一致",
            "最终检查存在缺失、重复、额外项或阶段错误",
        ),
        _check(
            "turn_completed",
            turn.status is TurnStatus.COMPLETED,
            "runtime",
            "Agent Turn 正常完成",
            "Agent Turn 未正常完成",
        ),
        _check(
            "behavior_checks_passed",
            _observation_set(behavior, task.behavior_checks, phase="final", passed=True),
            "correctness",
            "目标行为检查全部通过",
            "目标行为检查缺失、重复或失败",
        ),
        _check(
            "regression_checks_passed",
            _observation_set(regressions, task.regression_checks, phase="final", passed=True)
            if task.regression_checks
            else not regressions,
            "regression",
            "回归检查全部通过",
            "回归检查缺失、重复或失败",
        ),
        _check(
            "head_unchanged",
            git.head_revision == git.baseline_revision,
            "forbidden_edit",
            "Agent 未改写基线提交",
            "Agent 改写或切换了基线提交",
        ),
        _check(
            "allowed_changes",
            bool(changed)
            and changed <= allowed
            and not git.untracked_paths
            and not git.unsupported_change_paths
            and git.diff_observed_bytes > 0,
            "forbidden_edit",
            "实际变更均位于允许的已有普通文件",
            "存在空变更、越界、未跟踪或不支持的变更类型",
        ),
        _check(
            "change_count",
            0 < len(changed) <= task.max_changed_files,
            "forbidden_edit",
            "实际修改文件数在任务上限内",
            "实际修改文件数为空或超过任务上限",
        ),
        _check(
            "clean_index",
            not git.staged_paths,
            "forbidden_edit",
            "Git 暂存区保持干净",
            "Agent 修改了 Git 暂存区",
        ),
        _check(
            "test_feedback_order",
            _feedback_order(task, transcript),
            "correctness",
            "可见测试按失败、Patch、通过顺序形成闭环",
            "可见测试与 Patch 未形成可证明的反馈顺序",
        ),
        _check(
            "git_feedback_order",
            _git_feedback_order(task, transcript),
            "correctness",
            "通过测试后核对了 Git 状态和差异",
            "Git 核对缺失、失败或顺序不正确",
        ),
        _check(
            "final_answer_consistent",
            _answer_consistent(task, transcript, git, required_review_finding_ids),
            "final_answer",
            "结构化最终回答与实际路径和测试一致",
            "最终回答缺失、格式错误或与实际证据不一致",
        ),
        _check(
            "budget_respected",
            turn.model_steps <= task.budget.max_steps
            and turn.usage.total_tokens <= task.budget.max_tokens,
            "budget",
            "模型步骤与 Token 未超过任务预算",
            "模型步骤或 Token 超过任务预算",
        ),
    )
    categories, outcome = _eval_outcome(checks, repository_matched)
    return CodingEvalReport(
        run_id=run_id,
        task_id=task.task_id,
        task_version=task.task_version,
        task_fingerprint=task.fingerprint,
        outcome=outcome,
        failure_categories=categories,
        environment=environment,
        started_at=started_at,
        completed_at=completed_at,
        checks=checks,
        baseline_observations=baseline,
        final_observations=final,
        git=git,
        final_answer=_answer_evidence(transcript),
        metrics=EvalMetrics(
            elapsed_seconds=elapsed_seconds(started_at, completed_at),
            model_steps=turn.model_steps,
            input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            tool_calls=transcript.tool_calls,
            approvals=transcript.approvals,
            changed_files=len(changed),
        ),
    )
