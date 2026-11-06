"""判分汇入与双判分一致性：判分器必须在计划内，不一致逐个列出，预注册时间顺序受约束。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from evalkit.contracts import GraderSpec, RunPlan, Score, TrialRecord
from evalkit.errors import EvalkitError
from evalkit.grading import build_report, consistency
from pydantic import ValidationError

from tests.evalkit.builders import HARBOR_REWARD, PLAN_CREATED, harbor_plan

OFFICIAL = GraderSpec(grader_id="swebench-official", version="4.1.0", pass_score="resolved")
DONE = PLAN_CREATED + timedelta(hours=2)


class FixedGrader:
    """按槽位给出预设判定；None表示无法判分。"""

    def __init__(self, spec: GraderSpec, verdicts: dict[int, bool | None]) -> None:
        self.spec = spec
        self.verdicts = verdicts

    def grade(self, plan: RunPlan, trial: TrialRecord, evidence_root: Path) -> tuple[Score, ...]:
        verdict = self.verdicts.get(trial.slot)
        if verdict is None:
            return ()
        return (Score(name=self.spec.pass_score, value=verdict, grader_id=self.spec.grader_id),)


def trials(plan: RunPlan, *, started: datetime | None = None) -> tuple[TrialRecord, ...]:
    begin = started or PLAN_CREATED + timedelta(minutes=1)
    return tuple(
        TrialRecord(
            slot=slot,
            arm_id=arm,
            item_id=item,
            attempt=attempt,
            status="missing" if slot == 3 else "completed",
            reason="missing_result" if slot == 3 else "completed",
            started_at=None if slot == 3 else begin,
            completed_at=None if slot == 3 else begin + timedelta(minutes=1),
        )
        for slot, (arm, item, attempt) in enumerate(plan.slots())
    )


def dual_plan(**changes: object) -> RunPlan:
    return harbor_plan(
        4, graders=(OFFICIAL, HARBOR_REWARD), primary="swebench-official"
    ).model_copy(update=changes)


def test_report_runs_declared_graders_and_lists_every_disagreement(tmp_path: Path) -> None:
    plan = dual_plan()
    official = FixedGrader(OFFICIAL, {0: True, 1: False, 2: True})
    reward = FixedGrader(HARBOR_REWARD, {0: True, 1: True, 2: None})
    report = build_report(plan, trials(plan)[::-1], (official, reward), tmp_path, DONE)
    assert [t.slot for t in report.trials] == [0, 1, 2, 3]
    assert report.trials[3].scores == ()
    assert report.summary[0].model_dump() == {
        "arm_id": "A",
        "scheduled": 4,
        "passed": 2,
        "failed": 1,
        "invalid": 1,
    }
    result = consistency(report, "swebench-official", "harbor-reward")
    assert (result.compared, result.agreed) == (2, 1)
    assert [(d.slot, d.values) for d in result.disagreements] == [
        (1, (False, True)),
        (2, (True, None)),
    ]


def test_graders_must_match_the_plan_and_emit_their_own_identity(tmp_path: Path) -> None:
    plan = dual_plan()
    other_version = OFFICIAL.model_copy(update={"version": "9.9.9"})
    for graders in (
        (FixedGrader(other_version, {}),),
        (FixedGrader(OFFICIAL, {}), FixedGrader(OFFICIAL, {})),
    ):
        with pytest.raises(EvalkitError) as error:
            build_report(plan, trials(plan), graders, tmp_path, DONE)
        assert error.value.code == "grader_not_in_plan"

    impostor = FixedGrader(OFFICIAL, {0: True})
    impostor.spec = OFFICIAL
    impostor.grade = lambda *_: (  # type: ignore[method-assign]
        Score(name="reward_passed", value=True, grader_id="harbor-reward"),
    )
    with pytest.raises(EvalkitError) as error:
        build_report(plan, trials(plan), (impostor,), tmp_path, DONE)
    assert error.value.code == "grader_identity_invalid"
    with pytest.raises(EvalkitError):
        consistency(build_report(plan, trials(plan), (), tmp_path, DONE), "x", "harbor-reward")


def test_preregistered_plans_must_precede_every_trial(tmp_path: Path) -> None:
    early = PLAN_CREATED - timedelta(days=30)
    with pytest.raises(ValidationError, match="backfilled"):
        build_report(dual_plan(), trials(dual_plan(), started=early), (), tmp_path, DONE)
    backfilled = RunPlan.model_validate_json(
        dual_plan(provenance="backfilled", source_refs={"suite": "abc"}).model_dump_json()
    )
    report = build_report(backfilled, trials(backfilled, started=early), (), tmp_path, early)
    assert report.plan.provenance == "backfilled"


def test_scores_from_undeclared_graders_are_rejected(tmp_path: Path) -> None:
    plan = dual_plan()
    forged = list(trials(plan))
    forged[0] = TrialRecord.model_validate_json(
        forged[0]
        .model_copy(update={"scores": (Score(name="resolved", value=True, grader_id="llm-judge"),)})
        .model_dump_json()
    )
    with pytest.raises(ValidationError, match="计划外"):
        build_report(plan, tuple(forged), (), tmp_path, datetime.now(UTC))
