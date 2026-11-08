"""四个证据合同的核心规则：完整分母、汇总可重算、主指标判定、计划绑定。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from evalkit.contracts import (
    ArmSpec,
    Cost,
    DatasetItem,
    DatasetManifest,
    EvalRunReport,
    GraderSpec,
    RunPlan,
    Score,
    TrialRecord,
    summarize,
    trial_outcome,
)
from evalkit.steps import STEP_TEXT_LIMIT, Step, StepTrace, step_text
from pydantic import ValidationError

T0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def plan(
    attempts: int = 1,
    primary_grader: str = "swebench-official",
    version: str = "evalkit.run-plan/v1",
) -> RunPlan:
    return RunPlan(
        spec_version=version,
        run_name="e0-smoke",
        dataset=DatasetManifest(
            name="verified-dev",
            version="v1",
            items=(
                DatasetItem(
                    item_id="django__django-1", item_digest=DIGEST, labels=("repo:django",)
                ),
                DatasetItem(item_id="sympy__sympy-2", item_digest="sha256:" + "b" * 64),
            ),
        ),
        arms=(
            ArmSpec(
                arm_id="A2",
                system="harnessix",
                system_version="47d4c4bc",
                config_digest=DIGEST,
                executor="harbor",
            ),
            ArmSpec(
                arm_id="R",
                system="mini-swe-agent",
                system_version="1.0",
                config_digest=DIGEST,
                executor="harbor",
            ),
        ),
        attempts_per_item=attempts,
        graders=(
            GraderSpec(grader_id="swebench-official", version="5.0.2", pass_score="resolved"),
            GraderSpec(grader_id="harbor-reward", version="0.25.0", pass_score="reward_passed"),
        ),
        primary_grader=primary_grader,
        created_at=T0,
    )


def trial(slot: int, arm: str, item: str, attempt: int = 0, **changes: object) -> TrialRecord:
    values: dict[str, object] = {
        "slot": slot,
        "arm_id": arm,
        "item_id": item,
        "attempt": attempt,
        "status": "completed",
        "reason": "verified",
        "scores": (Score(name="resolved", value=True, grader_id="swebench-official"),),
        "started_at": T0,
        "completed_at": T0 + timedelta(minutes=5),
    }
    values.update(changes)
    return TrialRecord.model_validate(values)


def full_trials(p: RunPlan) -> tuple[TrialRecord, ...]:
    return tuple(trial(i, arm, item, attempt) for i, (arm, item, attempt) in enumerate(p.slots()))


def report(p: RunPlan, trials: tuple[TrialRecord, ...]) -> EvalRunReport:
    return EvalRunReport(
        plan=p,
        plan_digest=p.digest,
        trials=trials,
        summary=summarize(p, trials),
        completed_at=T0 + timedelta(hours=1),
    )


def test_slots_cover_arms_items_and_attempts_in_fixed_order() -> None:
    slots = plan(attempts=2).slots()
    assert len(slots) == 2 * 2 * 2
    assert slots[:3] == (
        ("A2", "django__django-1", 0),
        ("A2", "django__django-1", 1),
        ("A2", "sympy__sympy-2", 0),
    )


def test_report_requires_every_slot_even_missing_ones() -> None:
    p = plan()
    trials = full_trials(p)
    with pytest.raises(ValidationError, match="完整覆盖"):
        report(p, trials[:-1])
    missing = trials[:-1] + (
        trial(3, "R", "sympy__sympy-2", status="missing", reason="missing_result", scores=()),
    )
    built = report(p, missing)
    assert [s.model_dump() for s in built.summary][1] == {
        "arm_id": "R",
        "scheduled": 2,
        "passed": 1,
        "failed": 0,
        "invalid": 1,
    }


def test_summary_must_be_recomputable_from_trials() -> None:
    p = plan()
    trials = full_trials(p)
    forged = tuple(s.model_copy(update={"passed": 0}) for s in summarize(p, trials))
    with pytest.raises(ValidationError, match="汇总"):
        EvalRunReport(
            plan=p,
            plan_digest=p.digest,
            trials=trials,
            summary=forged,
            completed_at=T0 + timedelta(hours=1),
        )


def test_report_is_bound_to_the_frozen_plan_digest() -> None:
    p = plan()
    trials = full_trials(p)
    with pytest.raises(ValidationError, match="未绑定"):
        EvalRunReport(
            plan=p,
            plan_digest="sha256:" + "0" * 64,
            trials=trials,
            summary=summarize(p, trials),
            completed_at=T0 + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "passed"),
        (
            {"scores": (Score(name="resolved", value=False, grader_id="swebench-official"),)},
            "failed",
        ),
        (
            {"scores": (Score(name="reward_passed", value=True, grader_id="harbor-reward"),)},
            "invalid",
        ),
        ({"status": "infra_error", "reason": "image_pull_failed", "scores": ()}, "invalid"),
        ({"status": "timeout", "reason": "deadline_exceeded", "scores": ()}, "failed"),
        ({"status": "system_error", "reason": "provider_failed", "scores": ()}, "failed"),
    ],
)
def test_outcome_comes_only_from_the_primary_grader(
    changes: dict[str, object], expected: str
) -> None:
    p = plan()
    primary = next(g for g in p.graders if g.grader_id == p.primary_grader)
    assert trial_outcome(trial(0, "A2", "django__django-1", **changes), primary) == expected


@pytest.mark.parametrize("status", ["system_error", "timeout"])
@pytest.mark.parametrize(
    ("resolved", "strict", "lenient"),
    [(True, "failed", "passed"), (False, "failed", "failed"), (None, "failed", "failed")],
)
def test_v2_primary_counts_a_resolved_patch_even_without_normal_end(
    status: str, resolved: bool | None, strict: str, lenient: str
) -> None:
    """v1要求系统正常结束；v2只看判分，未判分的系统失败仍记失败而不是invalid。"""
    scores = (
        ()
        if resolved is None
        else (Score(name="resolved", value=resolved, grader_id="swebench-official"),)
    )
    record = trial(0, "A2", "django__django-1", status=status, reason="budget", scores=scores)
    primary = plan().graders[0]
    assert trial_outcome(record, primary) == strict
    assert trial_outcome(record, primary, require_normal_end=False) == lenient


def test_v2_report_summarizes_by_grader_and_keeps_stability_strict() -> None:
    p = plan(version="evalkit.run-plan/v2")
    trials = full_trials(p)
    budget = trials[0].model_copy(update={"status": "system_error", "reason": "budget_exceeded"})
    trials = (TrialRecord.model_validate(budget.model_dump()), *trials[1:])
    built = report(p, trials)
    assert not p.primary_requires_normal_end and plan().primary_requires_normal_end
    assert built.summary[0].passed == 2
    assert summarize(p, trials, require_normal_end=True)[0].passed == 1
    assert report(plan(), trials).summary[0].passed == 1


def test_plan_versions_share_fields_so_v1_digests_stay_unchanged() -> None:
    v1, v2 = plan(), plan(version="evalkit.run-plan/v2")
    assert set(v1.model_dump()) == set(v2.model_dump())
    assert v1.model_dump()["spec_version"] == "evalkit.run-plan/v1"
    assert v1.digest != v2.digest


def test_plan_rejects_unknown_primary_grader_and_duplicates() -> None:
    with pytest.raises(ValidationError, match="主指标"):
        plan(primary_grader="llm-judge")
    with pytest.raises(ValidationError, match="重复"):
        DatasetManifest(
            name="d",
            version="1",
            items=(
                DatasetItem(item_id="x", item_digest=DIGEST),
                DatasetItem(item_id="x", item_digest=DIGEST),
            ),
        )


def test_unknown_cost_stays_unknown() -> None:
    assert Cost().amount is None
    with pytest.raises(ValidationError, match="未知成本"):
        Cost(completeness="unknown", currency="CNY", amount="0.10")
    with pytest.raises(ValidationError, match="同时存在"):
        Cost(completeness="complete", currency="CNY")


def test_missing_trial_cannot_carry_scores() -> None:
    with pytest.raises(ValidationError, match="缺失试次"):
        trial(0, "A2", "django__django-1", status="missing", reason="missing_result")


def test_step_trace_order_parents_and_content_stripping() -> None:
    def step(step_id: str, minute: int, **changes: object) -> Step:
        values: dict[str, object] = {
            "step_id": step_id,
            "kind": "tool",
            "name": "read_file",
            "started_at": T0 + timedelta(minutes=minute),
            "completed_at": T0 + timedelta(minutes=minute, seconds=5),
            "status": "ok",
            "input_text": "secret prompt",
        }
        values.update(changes)
        return Step.model_validate(values)

    trace = StepTrace(
        plan_digest="p", slot=0, steps=(step("s1", 0), step("s2", 1, parent_step_id="s1"))
    )
    assert all(s.input_text is None for s in trace.without_content().steps)
    with pytest.raises(ValidationError, match="排序"):
        StepTrace(plan_digest="p", slot=0, steps=(step("s2", 1), step("s1", 0)))
    with pytest.raises(ValidationError, match="父步骤"):
        StepTrace(plan_digest="p", slot=0, steps=(step("s1", 0, parent_step_id="nope"),))
    with pytest.raises(ValidationError, match="只有模型调用"):
        step("s3", 2, model="qwen")


def test_step_text_labels_blocks_skips_empty_and_stays_within_the_step_limit() -> None:
    assert step_text([]) is None
    assert step_text([("assistant", ""), ("user", "")]) is None
    assert step_text([("user", "fix it"), ("assistant", ""), ("tool_call bash", "ls")]) == (
        "[user]\nfix it\n\n[tool_call bash]\nls"
    )
    body = "x" * (STEP_TEXT_LIMIT + 5000)
    clipped = step_text([("tool_result bash", body)])
    assert clipped is not None and len(clipped) <= STEP_TEXT_LIMIT
    kept = clipped.index("\n…[truncated ")
    dropped = len("[tool_result bash]\n") + len(body) - kept
    assert clipped.endswith(f"\n…[truncated {dropped} chars]")
    Step.model_validate(
        {
            "step_id": "big",
            "kind": "tool",
            "name": "bash",
            "started_at": T0,
            "completed_at": T0,
            "status": "ok",
            "output_text": clipped,
        }
    )
