"""官方SWE-bench判分：对试次登记的补丁批量运行`swebench eval`，把判定写回试次记录。

按组别与重复序号分批，每批每个条目一条预测；补丁先按登记摘要核对。官方报告中resolved为通过，
unresolved与空补丁为未通过；出错、基础设施失败、结果含糊或没有报告的试次不写判定，
主指标据此记为无效，不补造结果。原试次文件不修改，输出新的0600试次文件。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from evalkit.contracts import ArmTrials, GraderSpec, RunPlan, Score, TrialRecord
from evalkit.files import read_artifact, read_model, write_model

_PATCH = "patch"
_MAX_PATCH = 16 * 1024 * 1024
_MAX_FILE = 256 * 1024 * 1024
_VERDICTS = {
    "resolved_ids": (True, "resolved"),
    "unresolved_ids": (False, "unresolved"),
    "empty_patch_ids": (False, "empty_patch"),
}
_NO_VERDICT = ("error_ids", "infra_failure_ids", "ambiguous_failure_ids", "incomplete_ids")


def _predictions(
    trials: tuple[TrialRecord, ...], evidence_root: Path
) -> dict[tuple[str, int], list[dict]]:
    batches: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for trial in trials:
        ref = next((a for a in trial.artifacts if a.name == _PATCH), None)
        if trial.status == "missing" or ref is None:
            continue
        patch = read_artifact(ref, evidence_root, _MAX_PATCH).decode("utf-8")
        batches[(trial.arm_id, trial.attempt)].append(
            {
                "instance_id": trial.item_id,
                "model_name_or_path": f"{trial.arm_id}-attempt{trial.attempt}",
                "model_patch": patch,
            }
        )
    return batches


def _evaluate(
    batch: list[dict], name: str, work: Path, dataset: Path, swebench: str, workers: int
) -> dict[str, tuple[bool | None, str]]:
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
    report = json.loads(reports[0].read_text())
    verdicts: dict[str, tuple[bool | None, str]] = {}
    for key, verdict in _VERDICTS.items():
        verdicts.update({instance: verdict for instance in report.get(key, [])})
    for key in _NO_VERDICT:
        verdicts.update(
            {instance: (None, key.removesuffix("_ids")) for instance in report.get(key, [])}
        )
    return verdicts


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
        verdicts: dict[tuple[int, str], tuple[bool | None, str]] = {}
        for (arm, attempt), batch in sorted(_predictions(group.trials, evidence_root).items()):
            result = _evaluate(batch, f"{arm}-attempt{attempt}", work, dataset, swebench, workers)
            verdicts.update({(attempt, instance): value for instance, value in result.items()})
        trials = []
        for trial in group.trials:
            value, status = verdicts.get((trial.attempt, trial.item_id), (None, "not_graded"))
            scores = [Score(name="official_status", value=status, grader_id=spec.grader_id)]
            if value is not None:
                scores.append(Score(name=spec.pass_score, value=value, grader_id=spec.grader_id))
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
