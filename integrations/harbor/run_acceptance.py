"""重复执行完整本地验收矩阵及真实Langfuse闭环；只允许离线确定性产品夹具。

运行前设置PYTHONPATH为仓库根目录与packages/evalkit/src，使evalkit与Harnessix适配可导入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit.contracts import (
    ArmSpec,
    DatasetItem,
    DatasetManifest,
    EvalRunReport,
    GraderSpec,
    RunPlan,
    trial_outcome,
)
from evalkit.digest import bytes_digest
from evalkit.files import read_model, write_model

HARBOR_REWARD = GraderSpec(grader_id="harbor-reward", version="0.25.0", pass_score="reward_passed")
PARSER = "integrations.harnessix_eval.harbor_parser:HarnessixResultParser"


def _private_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")


def _run_cli(root: Path, arguments: list[str]) -> dict[str, Any]:
    environment = {**os.environ, "PYTHONPATH": f"{root}{os.pathsep}{root / 'packages/evalkit/src'}"}
    result = subprocess.run(
        [sys.executable, "-m", "evalkit", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("评测CLI未返回结构化结果，详查本机原始证据") from None
    if result.returncode:
        raise RuntimeError(f"评测CLI失败：{value.get('reason', 'unknown')}")
    return value


def _containers(image: str) -> list[str]:
    return subprocess.check_output(
        ["docker", "ps", "--filter", f"ancestor={image}", "--format", "{{.ID}}"],
        text=True,
        timeout=10,
    ).split()


def _monitor(process: subprocess.Popen, image: str, image_id: str) -> dict[str, Any]:
    observed = {}
    deadline = time.monotonic() + 900
    while process.poll() is None:
        if time.monotonic() > deadline:
            process.terminate()
            raise RuntimeError("Harbor验收超过总预算")
        for identity in _containers(image):
            try:
                value = subprocess.check_output(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{json .Name}}|{{json .Image}}|{{json .HostConfig.NetworkMode}}",
                        identity,
                    ],
                    text=True,
                    timeout=10,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                if identity not in _containers(image):
                    continue
                raise
            name, actual_image, network = (json.loads(part) for part in value.strip().split("|"))
            if actual_image != image_id or network != "none":
                process.terminate()
                raise RuntimeError("实际容器镜像或网络策略与验收声明不一致")
            observed[identity] = {"name": name, "image_id": actual_image, "network_mode": network}
        time.sleep(0.5)
    if process.returncode:
        raise RuntimeError("Harbor进程异常退出，保留原始Job证据")
    if not observed or _containers(image):
        raise RuntimeError("缺少真实容器证据或容器未清理")
    return observed


def _git_head(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _plan(
    root: Path, harbor_python: Path, tasks: Path, config_digest: str, revision: str | None
) -> RunPlan:
    """执行前冻结计划；任务摘要用Harbor自己的内容哈希计算，导入时与Job锁逐条核对。"""
    digests = json.loads(
        subprocess.check_output(
            [
                str(harbor_python),
                "-c",
                "import json, sys\n"
                "from pathlib import Path\n"
                "from harbor.publisher.packager import Packager\n"
                "print(json.dumps({p: Packager.compute_content_hash(Path(p))[0]"
                " for p in sys.argv[1:]}))",
                *(str(task) for task in sorted(tasks.iterdir())),
            ],
            text=True,
        )
    )
    return RunPlan(
        run_name="harbor-native-matrix",
        dataset=DatasetManifest(
            name="harbor-native-matrix",
            version="1",
            items=tuple(
                DatasetItem(item_id=Path(path).name, item_digest="sha256:" + digest)
                for path, digest in digests.items()
            ),
        ),
        arms=(
            ArmSpec(
                arm_id="harnessix",
                system="harnessix",
                system_version=revision or _git_head(root),
                config_digest=config_digest,
                executor="harbor",
            ),
        ),
        attempts_per_item=1,
        graders=(HARBOR_REWARD,),
        primary_grader=HARBOR_REWARD.grader_id,
        created_at=datetime.now(UTC),
    )


def _check_matrix(report: EvalRunReport, job: Path) -> None:
    expected = {
        "01-protocol-pass": ("completed", "completed", "passed"),
        "02-protocol-task-fail": ("completed", "completed", "failed"),
        "03-protocol-approval": ("system_error", "intervention_required", "failed"),
        "04-protocol-deadline": ("timeout", "deadline_exceeded", "failed"),
        "05-protocol-provider-fail": ("system_error", "provider_failed", "failed"),
    }
    summary = report.summary[0]
    if (summary.scheduled, summary.passed) != (5, 1):
        raise RuntimeError("完整分母或任务验收结果不符合冻结矩阵")
    for trial in report.trials:
        observed = (trial.status, trial.reason, trial_outcome(trial, HARBOR_REWARD))
        if observed != expected.pop(trial.item_id):
            raise RuntimeError("原生故障归因与受控场景不一致")
        if trial.executor_refs.get("verifier_mode") != "separate" or trial.cost.amount is not None:
            raise RuntimeError("独立验收或未知费用语义丢失")
    if expected:
        raise RuntimeError("受控场景缺失")
    for path in job.glob("*/result.json"):
        source = json.loads(path.read_text())
        native = source["agent_result"]["metadata"]["harnessix"]
        if not native["turn_terminal_confirmed"] and native["protocol_evidence_complete"]:
            raise RuntimeError("未确认终态却声称完整协议证据")
        if (
            source["agent_result"]["cost_usd"] is not None
            or source["agent_result"]["n_input_tokens"] is not None
        ):
            raise RuntimeError("不完整公共观测被误写为标准账单")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-receipt", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--langfuse-base-url", required=True)
    parser.add_argument("--allow-network", action="store_true", required=True)
    parser.add_argument(
        "--harbor-python",
        type=Path,
        help="Harbor独立环境的python（默认integrations/harbor/.venv/bin/python）",
    )
    parser.add_argument("--code-revision", help="代码目录不是Git检出时显式给出版本")
    parser.add_argument(
        "--harbor-network-mode",
        choices=("no-network", "public"),
        default="no-network",
        help="Harbor策略层网络模式；断网始终由Compose的network_mode: none保证并逐容器核对",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    harbor_python = args.harbor_python or root / "integrations/harbor/.venv/bin/python"
    output = args.output_directory.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    image = json.loads(args.image_receipt.read_text())
    actual = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image["image"]], text=True
    ).strip()
    if actual != image["image_id"]:
        raise RuntimeError("验收镜像被替换")
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("fixtures") / "prepare_tasks.py"),
            "--image-receipt",
            str(args.image_receipt.resolve()),
            "--output-directory",
            str(output / "tasks"),
            "--harbor-network-mode",
            args.harbor_network_mode,
        ],
        check=True,
    )
    config = {
        "job_name": "native-matrix",
        "jobs_dir": str(output / "jobs"),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "retry": {"max_retries": 0},
        "agents": [
            {
                "import_path": "integrations.harbor.harnessix_agent:HarnessixAgent",
                "kwargs": {
                    "server_config": "/opt/harnessix/product.json",
                    "python_executable": "/usr/local/bin/python",
                    "timeout_seconds": 15,
                    "max_steps": 4,
                    "max_tokens": 2000,
                },
            }
        ],
        "tasks": [{"path": str(task)} for task in sorted((output / "tasks").iterdir())],
    }
    _private_json(output / "job-config.json", config)
    plan_path = output / "plan.json"
    plan = _plan(
        root,
        harbor_python,
        output / "tasks",
        bytes_digest((output / "job-config.json").read_bytes()),
        args.code_revision,
    )
    write_model(plan_path, plan, 1024 * 1024)
    environment = {**os.environ, "HARBOR_TELEMETRY": "0", "TZ": "UTC", "PYTHONPATH": str(root)}
    with (output / "harbor.log").open("x") as log:
        process = subprocess.Popen(
            [
                str(harbor_python.with_name("harbor")),
                "run",
                "--config",
                str(output / "job-config.json"),
            ],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        containers = _monitor(process, image["image"], actual)
    _private_json(
        output / "container-evidence.json",
        {"observed_containers": containers, "container_cleanup_verified": True},
    )
    job = output / "jobs/native-matrix"
    _run_cli(
        root,
        [
            "import-harbor",
            "--plan",
            str(plan_path),
            "--arm",
            "harnessix",
            "--job-directory",
            str(job),
            "--evidence-root",
            str(output),
            "--parser",
            PARSER,
            "--source-timezone",
            "UTC",
            "--output",
            str(output / "trials.json"),
        ],
    )
    _run_cli(
        root,
        [
            "report",
            "--plan",
            str(plan_path),
            "--trials",
            str(output / "trials.json"),
            "--evidence-root",
            str(output),
            "--grader",
            "harbor-reward=evalkit.harbor:HarborRewardGrader",
            "--output",
            str(output / "run-report.json"),
        ],
    )
    report = read_model(output / "run-report.json", EvalRunReport, 64 * 1024 * 1024)
    _check_matrix(report, job)
    hashes_before = {
        str(path.relative_to(job)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in job.rglob("*.json")
    }
    command = [
        "export",
        "--report",
        str(output / "run-report.json"),
        "--output",
        str(output / "langfuse-export.json"),
        "--allow-network",
        "--langfuse-base-url",
        args.langfuse_base_url,
        "--verify",
        "--readback-report",
        str(output / "langfuse-readback.json"),
    ]
    readbacks = []
    for _ in range(2):
        readbacks.append(_run_cli(root, command)["readback"])
        time.sleep(3)
    after = {
        str(path.relative_to(job)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in job.rglob("*.json")
    }
    if (
        hashes_before != after
        or readbacks[0]["source_report_digest"] != readbacks[1]["source_report_digest"]
    ):
        raise RuntimeError("补发改变了源证据或实验身份")
    _private_json(
        output / "acceptance.json",
        {
            "status": "passed",
            "scenario_assertions_passed": 5,
            "task_passed": 1,
            "task_denominator": 5,
            "paid_model_calls": 0,
            "image": image,
            "observed_containers": containers,
            "container_cleanup_verified": True,
            "readbacks": readbacks,
            "source_files_unchanged_after_resend": True,
            "source_file_sha256": after,
        },
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "scenarios": 5,
                "task_pass_rate": "1/5",
                "receipt": str(output / "acceptance.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
