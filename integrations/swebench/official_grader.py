"""官方SWE-bench判分：对试次登记的补丁批量运行`swebench eval`，把判定写回试次记录。

按组别与重复序号分批，每批每个条目一条预测；补丁先按登记摘要核对。判定与官方报告同口径：
resolved为通过，unresolved与空补丁为未通过；出错或没有报告的试次不写判定，主指标据此记为无效，
不补造结果。swebench 5.0.2对未通过试次另做的基础设施/含糊失败分类只是参考（官方报告里这些试次
仍留在unresolved或error中，分母不变），这里同样不改判定，只把层级与原因作为诊断分数记下。
单个补丁缺失、超限、摘要不符或不是UTF-8时只影响该试次（不写判定并注明原因），不中止整批。
原试次文件不修改，输出新的0600试次文件。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from evalkit.contracts import ArmTrials, ArtifactRef, GraderSpec, RunPlan, Score, TrialRecord
from evalkit.errors import EvalkitError
from evalkit.files import read_artifact, read_model, write_model
from evalkit.harbor import OVERSIZED_ARTIFACTS_REF

_PATCH = "patch"
# SWE-bench补丁是整个仓库的改动，含二进制文件，导出时不裁剪；上限只挡住异常文件，
# 与evalkit导入的智能体制品上限相同（超限补丁在导入时已不登记，见_predictions）。
_MAX_PATCH = 64 * 1024 * 1024
_MAX_FILE = 256 * 1024 * 1024
_VERDICTS = {
    "resolved_ids": (True, "resolved"),
    "unresolved_ids": (False, "unresolved"),
    "empty_patch_ids": (False, "empty_patch"),
}
_NO_VERDICT = ("error_ids", "incomplete_ids")
# 官方的事后失败分类（swebench.harness.infra_failure）：只作诊断，不改变上面的判定。
_FAILURE_TIERS = {"infra_failure_ids": "environment", "ambiguous_failure_ids": "ambiguous"}
_PATCH_PROBLEMS = {
    "file_invalid": "patch_file_invalid",
    "artifact_digest_mismatch": "patch_digest_mismatch",
    "artifact_outside_root": "patch_outside_root",
}

# 一个试次的官方结论：通过判定（None为不写判定）、状态与诊断分数。
Verdict = tuple[bool | None, str, dict[str, str]]


def _read_patch(ref: ArtifactRef, evidence_root: Path) -> tuple[str | None, str | None]:
    """返回补丁正文，或读不出时该试次的官方状态。"""
    try:
        return read_artifact(ref, evidence_root, _MAX_PATCH).decode("utf-8"), None
    except EvalkitError as error:
        return None, _PATCH_PROBLEMS.get(error.code, "patch_unreadable")
    except UnicodeDecodeError:
        # 官方预测文件里补丁是JSON字符串，非UTF-8补丁无法原样提交。
        return None, "patch_not_utf8"


def _predictions(
    trials: tuple[TrialRecord, ...], evidence_root: Path
) -> tuple[dict[tuple[str, int], list[dict]], dict[tuple[int, str], Verdict]]:
    """按组别与重复分批的预测，以及补丁读不出、只能不写判定的试次。"""
    batches: dict[tuple[str, int], list[dict]] = defaultdict(list)
    rejected: dict[tuple[int, str], Verdict] = {}
    for trial in trials:
        ref = next((a for a in trial.artifacts if a.name == _PATCH), None)
        if trial.status == "missing":
            continue
        if ref is None:
            if _PATCH in trial.executor_refs.get(OVERSIZED_ARTIFACTS_REF, "").split(","):
                rejected[(trial.attempt, trial.item_id)] = (None, "patch_oversized", {})
            continue
        patch, problem = _read_patch(ref, evidence_root)
        if patch is None:
            rejected[(trial.attempt, trial.item_id)] = (None, str(problem), {})
            continue
        batches[(trial.arm_id, trial.attempt)].append(
            {
                "instance_id": trial.item_id,
                "model_name_or_path": f"{trial.arm_id}-attempt{trial.attempt}",
                "model_patch": patch,
            }
        )
    return batches, rejected


def report_verdicts(report: dict) -> dict[str, Verdict]:
    """官方报告的分类映射为判定；失败层级与原因只附为诊断。"""
    verdicts: dict[str, Verdict] = {}
    for key, (value, status) in _VERDICTS.items():
        verdicts.update({instance: (value, status, {}) for instance in report.get(key, [])})
    for key in _NO_VERDICT:
        verdicts.update(
            {instance: (None, key.removesuffix("_ids"), {}) for instance in report.get(key, [])}
        )
    reasons = report.get("failure_reasons", {})
    for key, tier in _FAILURE_TIERS.items():
        for instance in report.get(key, []):
            if instance in verdicts:
                value, status, _ = verdicts[instance]
                diagnostics = {"failure_tier": tier}
                if isinstance(reasons.get(instance), str):
                    diagnostics["failure_reason"] = reasons[instance]
                verdicts[instance] = (value, status, diagnostics)
    return verdicts


def _evaluate(
    batch: list[dict], name: str, work: Path, dataset: Path, swebench: str, workers: int
) -> dict[str, Verdict]:
    directory = work / name
    directory.mkdir(parents=True, exist_ok=False)
    predictions = directory / "predictions.jsonl"
    predictions.write_text("".join(json.dumps(row) + "\n" for row in batch))
    command = [swebench, "eval", str(dataset), "-p", str(predictions), "--run-id", name]
    command += ["-j", str(workers), "--report-dir", str(directory)]
    for row in batch:
        command += ["-i", row["instance_id"]]
    environment = {**os.environ, "HF_HUB_OFFLINE": "1"}
    with (directory / "evaluate.log").open("w") as log:
        subprocess.run(
            command,
            cwd=directory,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    reports = sorted(directory.glob("*.json"))
    if len(reports) != 1:
        return {}
    return report_verdicts(json.loads(reports[0].read_text()))


def _scores(verdict: Verdict, spec: GraderSpec) -> list[Score]:
    value, status, diagnostics = verdict
    scores = [Score(name="official_status", value=status, grader_id=spec.grader_id)]
    if value is not None:
        scores.append(Score(name=spec.pass_score, value=value, grader_id=spec.grader_id))
    scores += [
        Score(name=name, value=text, grader_id=spec.grader_id)
        for name, text in sorted(diagnostics.items())
    ]
    return scores


def grade(
    plan: RunPlan,
    arm_trials: list[ArmTrials],
    spec: GraderSpec,
    *,
    evidence_root: Path,
    work: Path,
    dataset: Path,
    swebench: str,
    workers: int,
) -> list[ArmTrials]:
    graded = []
    for group in arm_trials:
        if group.plan_digest != plan.digest:
            raise SystemExit("试次文件不属于该计划")
        batches, verdicts = _predictions(group.trials, evidence_root)
        for (arm, attempt), batch in sorted(batches.items()):
            result = _evaluate(batch, f"{arm}-attempt{attempt}", work, dataset, swebench, workers)
            verdicts.update({(attempt, instance): value for instance, value in result.items()})
        trials = []
        for trial in group.trials:
            verdict = verdicts.get((trial.attempt, trial.item_id), (None, "not_graded", {}))
            scores = _scores(verdict, spec)
            trials.append(
                TrialRecord.model_validate_json(
                    trial.model_copy(update={"scores": (*trial.scores, *scores)}).model_dump_json()
                )
            )
        graded.append(
            ArmTrials.model_validate_json(
                group.model_copy(update={"trials": tuple(trials)}).model_dump_json()
            )
        )
    return graded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--trials", type=Path, nargs="+", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--grader-id", default="swebench-official")
    parser.add_argument("--dataset", type=Path, required=True, help="固定的完整数据集JSONL")
    parser.add_argument("--swebench", default="swebench", help="swebench命令（独立环境）")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    plan = read_model(args.plan, RunPlan, 8 * 1024 * 1024)
    spec = next((g for g in plan.graders if g.grader_id == args.grader_id), None)
    if spec is None:
        raise SystemExit("判分器不在计划内")
    groups = [read_model(p, ArmTrials, _MAX_FILE, require_private=True) for p in args.trials]
    args.output_directory.mkdir(parents=True, exist_ok=False)
    graded = grade(
        plan,
        groups,
        spec,
        evidence_root=args.evidence_root,
        work=args.work_directory,
        dataset=args.dataset,
        swebench=args.swebench,
        workers=args.workers,
    )
    for source, group in zip(args.trials, graded, strict=True):
        write_model(args.output_directory / source.name, group, _MAX_FILE)
    summary = {
        g.arm_id: sum(
            1 for t in g.trials if (s := t.score(spec.grader_id, spec.pass_score)) and s.value
        )
        for g in graded
    }
    print(json.dumps({"reason": "graded", "resolved": summary}))


if __name__ == "__main__":
    main()
