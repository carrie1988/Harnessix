"""Harbor评测运行：先冻结运行计划，再逐组别运行Harbor；导入、判分与导出用evalkit命令完成。

freeze：读取运行说明（组别、判分器、重复次数）与已准备的任务目录，用Harbor自己的内容哈希作为
条目摘要，写出运行计划和每个组别的Harbor Job配置。组别配置摘要覆盖智能体配置、环境配置与任务
准备收据，计划冻结后任何一项改变都对应新的计划。
run：以零自动重试运行一个组别；每个组别一个Job，导入时按组别逐一绑定。
需在Harbor环境中运行，PYTHONPATH包含仓库根目录与packages/evalkit/src。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit.cli import _load
from evalkit.contracts import (
    ArmSpec,
    ArmTrials,
    DatasetItem,
    DatasetManifest,
    GraderSpec,
    RunPlan,
    summarize,
)
from evalkit.files import read_model, write_model
from evalkit.grading import build_report, consistency
from evalkit.harbor import import_harbor_job
from harbor.publisher.packager import Packager

from integrations.swebench.official_grader import grade as official_grade

_JOB_FILE = "job-configs"
_PLAN_VERSIONS = {
    "grader_and_normal_end": "evalkit.run-plan/v1",
    "grader": "evalkit.run-plan/v2",
}


def _canonical(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _private_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")


def _environment(spec: dict[str, Any], code_root: Path) -> dict[str, Any]:
    """仓库内的Compose覆盖文件按代码目录解析为绝对路径。"""
    environment = dict(spec.get("environment", {}))
    if "extra_docker_compose" in environment:
        environment["extra_docker_compose"] = [
            str(code_root / path) for path in environment["extra_docker_compose"]
        ]
    return environment


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"组别制品不存在或是链接：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plan_version(spec: dict[str, Any]) -> str:
    """运行说明的主指标口径对应计划版本；未声明时沿用v1（要求系统正常结束）。"""
    rule = spec.get("primary_outcome", "grader_and_normal_end")
    if rule not in _PLAN_VERSIONS:
        raise SystemExit(f"不支持的主指标口径：{rule}")
    return _PLAN_VERSIONS[rule]


# 以仓库相对路径登记的智能体文件：mini的配置文件，以及Harbor渲染任务说明用的提示模板。
_REPOSITORY_FILE_KWARGS = ("config_file", "prompt_template_path")


def _resolve_repository_files(arm: dict[str, Any], code_root: Path) -> None:
    """仓库相对的智能体文件解析到代码目录下，并作为组别制品按内容计入配置摘要。"""
    kwargs = arm["agent"].get("kwargs", {})
    for key in _REPOSITORY_FILE_KWARGS:
        path = kwargs.get(key)
        if path is not None and not Path(path).is_absolute():
            kwargs[key] = str(code_root / path)
            arm["artifacts"] = [*arm.get("artifacts", ()), kwargs[key]]


def freeze(
    spec_path: Path, tasks: Path, run_dir: Path, code_root: Path, code_revision: str
) -> RunPlan:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    environment = _environment(spec, code_root)
    receipt = tasks / "prepare-receipt.json"
    task_dirs = sorted(p for p in tasks.iterdir() if (p / "task.toml").is_file())
    items = tuple(
        DatasetItem(
            item_id=task.name,
            item_digest="sha256:" + Packager.compute_content_hash(task)[0],
            labels=(f"repo:{task.name.split('__')[0]}",),
        )
        for task in task_dirs
    )
    shared = {
        "environment": spec.get("environment", {}),
        "compose_env": spec.get("compose_env", {}),
        "tasks_receipt": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "code_revision": code_revision,
    }
    # 组别登记的宿主制品（安装包、配置模板）按内容计入配置摘要，路径相同而内容变化即不同组别。
    for arm in spec["arms"]:
        _resolve_repository_files(arm, code_root)
        arm["artifact_sha256"] = {
            path: _file_sha256(Path(path)) for path in arm.get("artifacts", ())
        }
    arms = tuple(
        ArmSpec(
            arm_id=arm["arm_id"],
            system=arm["system"],
            system_version=arm["system_version"],
            config_digest=_canonical(
                {"agent": arm["agent"], "artifacts": arm["artifact_sha256"], **shared}
            ),
            executor="harbor",
        )
        for arm in spec["arms"]
    )
    plan = RunPlan(
        spec_version=_plan_version(spec),
        run_name=spec["run_name"],
        dataset=DatasetManifest(
            name=spec["dataset"]["name"], version=spec["dataset"]["version"], items=items
        ),
        arms=arms,
        attempts_per_item=spec.get("attempts", 1),
        graders=tuple(GraderSpec(**grader) for grader in spec["graders"]),
        primary_grader=spec["primary_grader"],
        content_policy=spec.get("content_policy", "metadata"),
        created_at=datetime.now(UTC),
    )
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    (run_dir / _JOB_FILE).mkdir(mode=0o700)
    write_model(run_dir / "plan.json", plan, 8 * 1024 * 1024)
    _private_json(run_dir / "spec.json", spec)
    for arm in spec["arms"]:
        _private_json(
            run_dir / _JOB_FILE / f"{arm['arm_id']}.json",
            {
                "job_name": arm["arm_id"],
                "jobs_dir": str(run_dir / "jobs"),
                "n_attempts": spec.get("attempts", 1),
                "n_concurrent_trials": spec.get("n_concurrent_trials", 1),
                "retry": {"max_retries": 0},
                "environment": environment,
                "agents": [arm["agent"]],
                "tasks": [{"path": str(task)} for task in task_dirs],
            },
        )
    return plan


def run(run_dir: Path, arm: str, code_root: Path) -> int:
    config = run_dir / _JOB_FILE / f"{arm}.json"
    spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    compose_env = {
        key: str(code_root / value) if key.endswith("_CODE") else value
        for key, value in spec.get("compose_env", {}).items()
    }
    environment = {
        **os.environ,
        **compose_env,
        "HARBOR_TELEMETRY": "0",
        "TZ": "UTC",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "PYTHONPATH": f"{code_root}{os.pathsep}{code_root / 'packages/evalkit/src'}",
    }
    harbor = Path(sys.executable).with_name("harbor")
    with (run_dir / f"{arm}.harbor.log").open("x") as log:
        return subprocess.run(
            [str(harbor), "run", "--config", str(config)],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        ).returncode


def finish(run_dir: Path, parsers: dict[str, str], args: argparse.Namespace) -> dict:
    """导入全部组别、官方判分、奖励判分汇入报告、双判分一致性；每一步都落0600文件。"""
    plan = read_model(run_dir / "plan.json", RunPlan, 8 * 1024 * 1024)
    trials_dir = run_dir / "trials"
    trials_dir.mkdir(mode=0o700)
    groups = []
    for arm in plan.arms:
        parser = _load(parsers[arm.arm_id])()
        jobs = run_dir / "jobs" / arm.arm_id
        trials = import_harbor_job(
            jobs, plan, arm.arm_id, {parser.agent_name: parser}, run_dir, source_timezone="UTC"
        )
        group = ArmTrials(plan_digest=plan.digest, arm_id=arm.arm_id, trials=trials)
        write_model(trials_dir / f"{arm.arm_id}.json", group, 64 * 1024 * 1024)
        groups.append(group)
    official = next(g for g in plan.graders if g.grader_id == args.official_grader)
    graded = official_grade(
        plan,
        groups,
        official,
        evidence_root=run_dir,
        work=run_dir / "official",
        dataset=args.dataset,
        swebench=args.swebench,
        workers=args.workers,
    )
    reward = next(g for g in plan.graders if g.grader_id == args.reward_grader)
    report = build_report(
        plan,
        tuple(trial for group in graded for trial in group.trials),
        (_load(args.reward_grader_class)(reward),),
        run_dir,
        datetime.now(UTC),
    )
    write_model(run_dir / "run-report.json", report, 64 * 1024 * 1024)
    agreement = consistency(report, official.grader_id, reward.grader_id)
    write_model(run_dir / "consistency.json", agreement, 8 * 1024 * 1024)
    _private_json(
        run_dir / "finish-receipt.json",
        {
            "finish_code_revision": args.code_revision,
            "dataset": str(args.dataset),
            "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
            "swebench": args.swebench,
            "parsers": parsers,
            "reward_grader_class": args.reward_grader_class,
        },
    )
    stability = (
        {}
        if plan.primary_requires_normal_end
        else {
            "normal_end_summary": [
                s.model_dump() for s in summarize(plan, report.trials, require_normal_end=True)
            ]
        }
    )
    return {
        "summary": [s.model_dump() for s in report.summary],
        **stability,
        "compared": agreement.compared,
        "agreed": agreement.agreed,
        "disagreements": [d.model_dump() for d in agreement.disagreements],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    frozen = commands.add_parser("freeze")
    frozen.add_argument("--spec", type=Path, required=True)
    frozen.add_argument("--tasks", type=Path, required=True)
    frozen.add_argument("--run-dir", type=Path, required=True)
    frozen.add_argument("--code-root", type=Path, required=True)
    frozen.add_argument("--code-revision", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--run-dir", type=Path, required=True)
    runner.add_argument("--arm", required=True)
    runner.add_argument("--code-root", type=Path, required=True)
    finisher = commands.add_parser("finish")
    finisher.add_argument("--run-dir", type=Path, required=True)
    finisher.add_argument("--parser", action="append", required=True, help="组别=模块:类")
    finisher.add_argument("--official-grader", default="swebench-official")
    finisher.add_argument("--reward-grader", default="harbor-reward")
    finisher.add_argument(
        "--reward-grader-class",
        default="integrations.harbor.swebench_reward:SwebenchRewardGrader",
    )
    finisher.add_argument("--dataset", type=Path, required=True)
    finisher.add_argument("--swebench", required=True)
    finisher.add_argument("--workers", type=int, default=4)
    finisher.add_argument("--code-revision", required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        plan = freeze(args.spec, args.tasks, args.run_dir, args.code_root, args.code_revision)
        print(
            json.dumps({"reason": "frozen", "plan_digest": plan.digest, "slots": len(plan.slots())})
        )
    elif args.command == "finish":
        parsers = dict(spec.split("=", 1) for spec in args.parser)
        print(json.dumps(finish(args.run_dir, parsers, args), ensure_ascii=False))
    else:
        code = run(args.run_dir, args.arm, args.code_root)
        print(json.dumps({"reason": "harbor_finished", "arm": args.arm, "exit_code": code}))


if __name__ == "__main__":
    main()
