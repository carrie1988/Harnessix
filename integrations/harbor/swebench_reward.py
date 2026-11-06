"""SWE-bench任务的Harbor奖励判分器：只有判分脚本写出报告且与奖励一致时才给出判定。

Harbor的SWE-bench判分脚本在解析器崩溃（例如无法下载依赖清单）时同样写0分，和真实未通过
无法区分。本判分器要求verifier/report.json存在，且其中resolved与reward==1一致，否则只记录
verifier_status，不写通过判定，主指标据此记为无效。
"""

from __future__ import annotations

import json
from pathlib import Path

from evalkit.contracts import GraderSpec, RunPlan, Score, TrialRecord
from evalkit.files import read_artifact
from evalkit.harbor import HarborRewardGrader

_REPORT = "harbor_verifier_report"


class SwebenchRewardGrader(HarborRewardGrader):
    def __init__(self, spec: GraderSpec) -> None:
        super().__init__(spec)

    def grade(self, plan: RunPlan, trial: TrialRecord, evidence_root: Path) -> tuple[Score, ...]:
        scores = super().grade(plan, trial, evidence_root)
        if not scores:
            return ()
        status = self._status(trial, evidence_root, scores[0].value)
        verdict = (Score(name="verifier_status", value=status, grader_id=self.spec.grader_id),)
        return verdict + scores if status == "consistent" else verdict

    def _status(self, trial: TrialRecord, evidence_root: Path, passed: object) -> str:
        ref = next((a for a in trial.artifacts if a.name == _REPORT), None)
        if ref is None:
            return "report_missing"
        try:
            report = json.loads(read_artifact(ref, evidence_root, 1 << 20))
            resolved = report[trial.item_id]["resolved"]
        except (KeyError, TypeError, ValueError):
            return "report_invalid"
        return "consistent" if resolved is passed else "report_disagrees"
