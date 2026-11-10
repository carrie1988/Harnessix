"""官方SWE-bench判分步骤：补丁按摘要核对，报告分类映射为判定，无法判定的试次不写结果；
官方的基础设施/含糊失败分类只作诊断，不改变判定。"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from evalkit.contracts import ArmTrials, GraderSpec, TrialRecord
from evalkit.errors import EvalkitError
from evalkit.files import artifact_ref

from integrations.swebench.official_grader import grade, report_verdicts
from tests.evalkit.builders import harbor_plan

OFFICIAL = GraderSpec(
    grader_id="swebench-official", version="swebench-5.0.2", pass_score="resolved"
)
PLAN = harbor_plan(5, graders=(OFFICIAL,), primary="swebench-official")
REPORT = {
    "resolved_ids": ["case-0"],
    "unresolved_ids": ["case-1"],
    "empty_patch_ids": ["case-2"],
    "error_ids": ["case-3"],
    # swebench 5.0.2的事后分类：被标记的试次仍留在unresolved/error中。
    "ambiguous_failure_ids": ["case-1"],
    "infra_failure_ids": ["case-3"],
    "failure_reasons": {"case-1": "missing_module", "case-3": "container_unavailable"},
}


def fake_swebench(tmp_path: Path) -> str:
    """模拟swebench eval：把固定报告写入--report-dir，并记录收到的预测。"""
    script = tmp_path / "swebench"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv\n"
        "report_dir = args[args.index('--report-dir') + 1]\n"
        "preds = [json.loads(l) for l in open(args[args.index('-p') + 1])]\n"
        f"report = {REPORT!r}\n"
        "submitted = {p['instance_id'] for p in preds}\n"
        "report = {k: ({i: r for i, r in v.items() if i in submitted} if isinstance(v, dict)"
        " else [i for i in v if i in submitted]) for k, v in report.items()}\n"
        "json.dump(report, open(report_dir + '/model.run.json', 'w'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def trials(evidence: Path, with_patch: set[int]) -> ArmTrials:
    records = []
    for slot, (arm, item, attempt) in enumerate(PLAN.slots()):
        artifacts = ()
        if slot in with_patch:
            path = evidence / f"{item}.diff"
            path.write_text("" if item == "case-2" else f"diff --git a/{item} b/{item}\n")
            artifacts = (artifact_ref("patch", path, evidence, 1 << 20),)
        records.append(
            TrialRecord(
                slot=slot,
                arm_id=arm,
                item_id=item,
                attempt=attempt,
                status="completed",
                reason="completed",
                artifacts=artifacts,
            )
        )
    return ArmTrials(plan_digest=PLAN.digest, arm_id="A", trials=tuple(records))


def run_grade(tmp_path: Path, group: ArmTrials) -> ArmTrials:
    (tmp_path / "work").mkdir()
    (graded,) = grade(
        PLAN,
        [group],
        OFFICIAL,
        evidence_root=tmp_path / "evidence",
        work=tmp_path / "work",
        dataset=tmp_path / "dataset.jsonl",
        swebench=fake_swebench(tmp_path),
        workers=1,
    )
    return graded


def test_report_categories_map_to_verdicts_and_unjudged_trials_stay_unscored(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    graded = run_grade(tmp_path, trials(evidence, {0, 1, 2, 3}))
    verdicts = [
        (
            t.score("swebench-official", "official_status").value,
            getattr(t.score("swebench-official", "resolved"), "value", None),
            getattr(t.score("swebench-official", "failure_tier"), "value", None),
            getattr(t.score("swebench-official", "failure_reason"), "value", None),
        )
        for t in graded.trials
    ]
    assert verdicts == [
        ("resolved", True, None, None),
        # 含糊失败与官方同口径仍是未通过，原因只作诊断。
        ("unresolved", False, "ambiguous", "missing_module"),
        ("empty_patch", False, None, None),
        ("error", None, "environment", "container_unavailable"),
        ("not_graded", None, None, None),
    ]
    predictions = (tmp_path / "work/A-attempt0/predictions.jsonl").read_text().splitlines()
    assert [json.loads(line)["instance_id"] for line in predictions] == [
        "case-0",
        "case-1",
        "case-2",
        "case-3",
    ]


def test_changed_patch_is_rejected_before_grading(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    group = trials(evidence, {0})
    (evidence / "case-0.diff").write_text("tampered\n")
    with pytest.raises(EvalkitError) as error:
        run_grade(tmp_path, group)
    assert error.value.code == "artifact_digest_mismatch"


def test_environment_tier_keeps_the_official_unresolved_verdict() -> None:
    """环境层失败与官方同口径：报告给出unresolved就记未通过，不因分类改为无效。"""
    verdicts = report_verdicts(
        {
            "unresolved_ids": ["oom"],
            "infra_failure_ids": ["oom"],
            "failure_reasons": {"oom": "out_of_memory"},
        }
    )
    assert verdicts == {
        "oom": (
            False,
            "unresolved",
            {"failure_tier": "environment", "failure_reason": "out_of_memory"},
        )
    }
