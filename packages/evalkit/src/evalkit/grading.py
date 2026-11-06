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
)


class Grader(Protocol):
    """读取试次与冻结制品，输出带本判分器身份的分数。"""

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
    """对全部完成的试次运行计划内的判分器，按槽位重排后生成可重算报告。"""
    raise NotImplementedError("判分汇入（ADR 0120 决策4）")


class Disagreement(Contract):
    slot: int
    arm_id: Identifier
    item_id: Identifier
    values: tuple[bool | None, bool | None]


class ConsistencyReport(Contract):
    plan_digest: Digest
    graders: tuple[Identifier, Identifier]
    compared: int = Field(ge=0)
    agreed: int = Field(ge=0)
    disagreements: tuple[Disagreement, ...]


def consistency(report: EvalRunReport, first: str, second: str) -> ConsistencyReport:
    """逐试次比较两个判分器的通过判定，逐个列出不一致与单边缺失。"""
    raise NotImplementedError("双判分一致性（ADR 0120 决策4）")
