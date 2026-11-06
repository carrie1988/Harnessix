"""判分器接口、分数汇入报告和双判分一致性；不把多个判分器合并成单一数字。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import Field

from evalkit.contracts import (
    Contract,
    Digest,
    EvalRunReport,
    GraderSpec,
    Identifier,
    RunPlan,
    Score,
    TrialRecord,
    summarize,
)
from evalkit.errors import EvalkitError


class Grader(Protocol):
    """读取试次与冻结制品，输出带本判分器身份的分数；不能判分时返回空元组。"""

    spec: GraderSpec

    def grade(
        self, plan: RunPlan, trial: TrialRecord, evidence_root: Path
    ) -> tuple[Score, ...]: ...


def build_report(
    plan: RunPlan,
    trials: tuple[TrialRecord, ...],
    graders: tuple[Grader, ...],
    evidence_root: Path,
    completed_at: datetime,
) -> EvalRunReport:
    """对除缺失外的全部试次运行给定判分器，按槽位排序后生成可重算报告。

    判分器必须是计划声明的同一版本；试次已带的分数（如在别处运行的判分器）原样保留。
    """
    declared = {grader.grader_id: grader for grader in plan.graders}
    ids = [grader.spec.grader_id for grader in graders]
    if len(set(ids)) != len(ids) or any(declared.get(g.spec.grader_id) != g.spec for g in graders):
        raise EvalkitError("grader_not_in_plan", "判分器重复，或与计划声明的身份和版本不一致")
    graded = []
    for trial in sorted(trials, key=lambda t: t.slot):
        scores = list(trial.scores)
        if trial.status != "missing":
            for grader in graders:
                produced = grader.grade(plan, trial, evidence_root)
                if any(score.grader_id != grader.spec.grader_id for score in produced):
                    raise EvalkitError("grader_identity_invalid", "判分器输出了其他判分器的分数")
                scores.extend(produced)
        graded.append(
            TrialRecord.model_validate_json(
                trial.model_copy(update={"scores": tuple(scores)}).model_dump_json()
            )
        )
    ordered = tuple(graded)
    return EvalRunReport(
        plan=plan,
        plan_digest=plan.digest,
        trials=ordered,
        summary=summarize(plan, ordered),
        completed_at=completed_at,
    )


class Disagreement(Contract):
    """一个试次上两个判分器的通过判定；None表示该判分器没有给出布尔判定。"""

    slot: int = Field(ge=0)
    arm_id: Identifier
    item_id: Identifier
    attempt: int = Field(ge=0)
    values: tuple[bool | None, bool | None]


class ConsistencyReport(Contract):
    plan_digest: Digest
    graders: tuple[Identifier, Identifier]
    compared: int = Field(ge=0, description="两个判分器都给出布尔判定的试次数")
    agreed: int = Field(ge=0)
    disagreements: tuple[Disagreement, ...] = Field(
        description="判定不同或只有一方给出判定的全部试次，逐个列出"
    )


def _pass_value(trial: TrialRecord, grader: GraderSpec) -> bool | None:
    score = trial.score(grader.grader_id, grader.pass_score)
    return score.value if score is not None and isinstance(score.value, bool) else None


def consistency(report: EvalRunReport, first: str, second: str) -> ConsistencyReport:
    """逐试次比较两个判分器的通过判定；双方都没有判定的试次不计入比较。"""
    graders = {grader.grader_id: grader for grader in report.plan.graders}
    if first == second or first not in graders or second not in graders:
        raise EvalkitError("grader_not_in_plan", "一致性比较需要计划内的两个不同判分器")
    compared = agreed = 0
    disagreements = []
    for trial in report.trials:
        values = (_pass_value(trial, graders[first]), _pass_value(trial, graders[second]))
        if values == (None, None):
            continue
        if None not in values:
            compared += 1
            agreed += values[0] == values[1]
        if values[0] != values[1]:
            disagreements.append(
                Disagreement(
                    slot=trial.slot,
                    arm_id=trial.arm_id,
                    item_id=trial.item_id,
                    attempt=trial.attempt,
                    values=values,
                )
            )
    return ConsistencyReport(
        plan_digest=report.plan_digest,
        graders=(first, second),
        compared=compared,
        agreed=agreed,
        disagreements=tuple(disagreements),
    )
