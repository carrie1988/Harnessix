"""通过正式评分报告验证最后一次成功修改后的测试与Git反馈闭环。"""

from __future__ import annotations

import json

import pytest

from harnessix.agent.models import (
    AgentFailure,
    Item,
    ItemStatus,
    TextContent,
    ToolResultContent,
)
from harnessix.domain.models import EffectClass
from harnessix.evals.contracts import CodingEvalReport
from tests.evals.test_grader import (
    CHANGED_PATH,
    completed_turn,
    grade,
    item,
    task,
    tool_pair,
)

_FEEDBACK_CODES = {"test_feedback_order", "git_feedback_order"}
_INCOMPLETE_RESULTS = (
    "missing-result",
    "pending-call",
    "pending-result",
    "failed-result-item",
    "cancelled-result-item",
)


def _profile_pair(profile: str, passed: bool) -> tuple[Item, Item]:
    return tool_pair(
        "run_tests",
        output={"profile": profile, "passed": passed},
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
    )


def _applied_patch() -> tuple[Item, Item]:
    """复用已有成功Patch效果，创建独立的调用和结果身份。"""
    result = completed_turn().items[4].content
    assert isinstance(result, ToolResultContent) and result.patch is not None
    return tool_pair("apply_patch", effect=EffectClass.NON_IDEMPOTENT_WRITE, patch=result.patch)


def _first_cycle(profiles: tuple[str, ...] = ("unit",)) -> tuple[Item, ...]:
    return (
        *(entry for profile in profiles for entry in _profile_pair(profile, False)),
        *_applied_patch(),
        *(entry for profile in profiles for entry in _profile_pair(profile, True)),
    )


def _git_review() -> tuple[Item, ...]:
    return (
        *tool_pair("git_status", output={"total_entries": 1}),
        *tool_pair("git_diff", output={"observed_sha256": "a" * 64}),
    )


def _incomplete_pair(pair: tuple[Item, Item], state: str) -> tuple[Item, ...]:
    call, result = pair
    if state == "missing-result":
        return (call,)
    if state == "pending-call":
        return (call.model_copy(update={"status": ItemStatus.STARTED}), result)
    status = {
        "pending-result": ItemStatus.STARTED,
        "failed-result-item": ItemStatus.FAILED,
        "cancelled-result-item": ItemStatus.CANCELLED,
    }[state]
    return (call, result.model_copy(update={"status": status}))


def _grade_feedback(
    *entries: Item,
    profiles: tuple[str, ...] = ("unit",),
    answer_at: int | None = None,
) -> CodingEvalReport:
    """保持v1合同及阳性外部Observation，只改变实际Session反馈顺序。"""
    original = completed_turn(changed_path=CHANGED_PATH)
    content = original.items[-1].content
    assert isinstance(content, TextContent)
    answer = json.loads(content.text)
    answer["tests"] = [{"profile": profile, "passed": True} for profile in profiles]
    final = item(TextContent(kind="assistant_message", text=json.dumps(answer)))
    position = len(entries) if answer_at is None else answer_at
    current_task = task(required_test_profiles=profiles)
    report = grade(
        current_task=current_task,
        turn=original.model_copy(
            update={
                "items": (
                    original.items[0],
                    *entries[:position],
                    final,
                    *entries[position:],
                )
            }
        ),
    )
    assert current_task.spec_version == "harnessix.coding-eval/v1"
    assert report.spec_version == "harnessix.coding-eval-report/v1"
    assert report.grader_version == "coding-eval-grader/v1"
    return report


def _assert_feedback(report: CodingEvalReport, *, tests_pass: bool, git_pass: bool) -> None:
    """同时核对两个正式Check、失败分类及整体结论，排除其他评分条件干扰。"""
    assert report.final_observations and all(entry.passed for entry in report.final_observations)
    assert report.git.changed_paths == (CHANGED_PATH,)
    assert all(check.passed for check in report.checks if check.code not in _FEEDBACK_CODES)
    assert {
        check.code: check.passed for check in report.checks if check.code in _FEEDBACK_CODES
    } == {"test_feedback_order": tests_pass, "git_feedback_order": git_pass}
    if tests_pass and git_pass:
        assert report.outcome == "passed" and not report.failure_categories
    else:
        assert report.outcome == "failed" and report.failure_categories == ("correctness",)


def test_second_successful_patch_requires_retest_despite_positive_final_observations() -> None:
    report = _grade_feedback(*_first_cycle(), *_applied_patch(), *_git_review())
    _assert_feedback(report, tests_pass=False, git_pass=False)


def test_second_successful_patch_passes_after_retest_and_git_review() -> None:
    report = _grade_feedback(
        *_first_cycle(), *_applied_patch(), *_profile_pair("unit", True), *_git_review()
    )
    _assert_feedback(report, tests_pass=True, git_pass=True)


@pytest.mark.parametrize(
    "retested_profiles",
    (("unit",), ("lint",), ("lint", "unit"), ("unit", "lint")),
    ids=("unit-only", "lint-only", "all-lint-unit", "all-unit-lint"),
)
def test_every_required_profile_must_be_retested_after_last_patch(
    retested_profiles: tuple[str, ...],
) -> None:
    profiles = ("lint", "unit")
    report = _grade_feedback(
        *_first_cycle(profiles),
        *_applied_patch(),
        *(entry for profile in retested_profiles for entry in _profile_pair(profile, True)),
        *_git_review(),
        profiles=profiles,
    )
    complete = set(retested_profiles) == set(profiles)
    _assert_feedback(report, tests_pass=complete, git_pass=complete)


@pytest.mark.parametrize("profiles", (("unit",), ("lint", "unit")), ids=("unit", "lint-unit"))
@pytest.mark.parametrize("fresh_pass", (False, True), ids=("no-fresh-pass", "pass-then-fail"))
def test_failed_last_check_invalidates_test_and_git_feedback(
    profiles: tuple[str, ...], fresh_pass: bool
) -> None:
    passed = (
        tuple(entry for profile in profiles for entry in _profile_pair(profile, True))
        if fresh_pass
        else ()
    )
    report = _grade_feedback(
        *_first_cycle(profiles),
        *_applied_patch(),
        *passed,
        *_profile_pair(profiles[0], False),
        *_git_review(),
        profiles=profiles,
    )
    _assert_feedback(report, tests_pass=False, git_pass=False)


@pytest.mark.parametrize("state", _INCOMPLETE_RESULTS)
def test_missing_or_nonterminal_retest_cannot_replace_stale_pass(state: str) -> None:
    report = _grade_feedback(
        *_first_cycle(),
        *_applied_patch(),
        *_incomplete_pair(_profile_pair("unit", True), state),
        *_git_review(),
    )
    _assert_feedback(report, tests_pass=False, git_pass=False)


@pytest.mark.parametrize("state", _INCOMPLETE_RESULTS + ("failed", "denied", "not-applied"))
def test_patch_without_successful_effect_does_not_invalidate_verified_first_patch(
    state: str,
) -> None:
    call, result = _applied_patch()
    if state in _INCOMPLETE_RESULTS:
        attempted = _incomplete_pair((call, result), state)
    else:
        content = result.content
        assert isinstance(content, ToolResultContent) and content.patch is not None
        effect_state = {"failed": "failed", "denied": "rejected", "not-applied": "approved"}[state]
        attempted = (
            call,
            item(
                ToolResultContent(
                    call_id=content.call_id,
                    outcome="succeeded" if state == "not-applied" else "failed",
                    patch=content.patch.model_copy(update={"state": effect_state}),
                    error=AgentFailure(code="approval_denied", message="Patch审批被拒绝")
                    if state == "denied"
                    else None,
                )
            ),
        )
    report = _grade_feedback(*_first_cycle(), *attempted, *_git_review())
    _assert_feedback(report, tests_pass=True, git_pass=True)


@pytest.mark.parametrize(
    "order",
    (
        ("unit", "status", "lint", "diff", "answer"),
        ("unit", "status", "diff", "lint", "answer"),
        ("unit", "lint", "diff", "status", "answer"),
        ("unit", "lint", "answer", "status", "diff"),
        ("unit", "lint", "status", "answer", "diff"),
    ),
    ids=(
        "status-before-all-tests",
        "diff-before-all-tests",
        "diff-before-status",
        "answer-before-status",
        "answer-before-diff",
    ),
)
def test_git_review_follows_all_final_passes_and_answer_follows_review(
    order: tuple[str, ...],
) -> None:
    profiles = ("lint", "unit")
    prefix = (*_first_cycle(profiles), *_applied_patch())
    blocks = {
        "unit": _profile_pair("unit", True),
        "lint": _profile_pair("lint", True),
        "status": tool_pair("git_status", output={"total_entries": 1}),
        "diff": tool_pair("git_diff", output={"observed_sha256": "a" * 64}),
        "answer": (),
    }
    answer_at = len(prefix) + sum(len(blocks[name]) for name in order[: order.index("answer")])
    report = _grade_feedback(
        *prefix,
        *(entry for name in order for entry in blocks[name]),
        profiles=profiles,
        answer_at=answer_at,
    )
    _assert_feedback(report, tests_pass=True, git_pass=False)


def test_single_patch_v1_feedback_remains_compatible() -> None:
    report = grade(current_task=task(), turn=completed_turn(changed_path=CHANGED_PATH))
    _assert_feedback(report, tests_pass=True, git_pass=True)
    assert report.spec_version == "harnessix.coding-eval-report/v1"
    assert report.grader_version == "coding-eval-grader/v1"
