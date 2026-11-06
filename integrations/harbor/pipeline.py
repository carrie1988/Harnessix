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

from evalkit.contracts import ArmSpec, DatasetItem, DatasetManifest, GraderSpec, RunPlan
from evalkit.files import write_model
from harbor.publisher.packager import Packager

_JOB_FILE = "job-configs"


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
    arms = tuple(
        ArmSpec(
            arm_id=arm["arm_id"],
            system=arm["system"],
            system_version=arm["system_version"],
            config_digest=_canonical({"agent": arm["agent"], **shared}),
            executor="harbor",
        )
        for arm in spec["arms"]
    )
    plan = RunPlan(
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
    args = parser.parse_args()
    if args.command == "freeze":
        plan = freeze(args.spec, args.tasks, args.run_dir, args.code_root, args.code_revision)
        print(
            json.dumps({"reason": "frozen", "plan_digest": plan.digest, "slots": len(plan.slots())})
        )
    else:
        code = run(args.run_dir, args.arm, args.code_root)
        print(json.dumps({"reason": "harbor_finished", "arm": args.arm, "exit_code": code}))


if __name__ == "__main__":
    main()
