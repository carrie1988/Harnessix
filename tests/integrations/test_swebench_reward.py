"""SWE-bench奖励判分器：报告缺失或与奖励不一致时不给通过判定，避免把判分崩溃记成未通过。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evalkit.contracts import GraderSpec
from evalkit.harbor import import_harbor_job

from integrations.harbor.swebench_reward import SwebenchRewardGrader
from tests.evalkit.builders import Trial, harbor_job, harbor_plan

SPEC = GraderSpec(grader_id="harbor-reward", version="harbor-0.25.0", pass_score="reward_passed")


@pytest.mark.parametrize(
    ("reward", "report", "expected"),
    [
        (1.0, True, ("consistent", True)),
        (0.0, False, ("consistent", False)),
        (0.0, None, ("report_missing", None)),
        (0.0, True, ("report_disagrees", None)),
    ],
)
def test_reward_needs_a_consistent_verifier_report(
    tmp_path: Path, reward: float, report: bool | None, expected: tuple
) -> None:
    job = harbor_job(tmp_path / "job", [Trial(reward=reward)])
    if report is not None:
        verifier = job / "trial-0-0" / "verifier"
        verifier.mkdir()
        (verifier / "report.json").write_text(json.dumps({"case-0": {"resolved": report}}))
    plan = harbor_plan(1, graders=(SPEC,), primary="harbor-reward")
    (trial,) = import_harbor_job(job, plan, "A", {}, tmp_path)
    scores = {s.name: s.value for s in SwebenchRewardGrader(SPEC).grade(plan, trial, tmp_path)}
    assert (scores["verifier_status"], scores.get("reward_passed")) == expected
