"""重复执行完整本地验收矩阵及真实Langfuse闭环；只允许离线确定性产品夹具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _private_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")


def _run_cli(root: Path, arguments: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "harnessix", *arguments], cwd=root, capture_output=True, text=True
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


def _check_matrix(report: dict[str, Any], job: Path) -> None:
    expected = {
        "01-protocol-pass": ("passed", "passed"),
        "02-protocol-task-fail": ("failed", "task"),
        "03-protocol-approval": ("failed", "runtime"),
        "04-protocol-deadline": ("failed", "budget"),
        "05-protocol-provider-fail": ("failed", "provider"),
    }
    if report["summary"]["scheduled_trials"] != 5 or report["summary"]["passed_trials"] != 1:
        raise RuntimeError("完整分母或任务验收结果不符合冻结矩阵")
    for trial in report["trials"]:
        if (trial["outcome"], trial["classification"]) != expected.pop(trial["task_name"]):
            raise RuntimeError("原生故障归因与受控场景不一致")
        if trial["verifier_mode"] != "separate" or trial["cost_completeness"] != "unknown":
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
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
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
    environment = {**os.environ, "HARBOR_TELEMETRY": "0", "TZ": "UTC", "PYTHONPATH": str(root)}
    with (output / "harbor.log").open("x") as log:
        process = subprocess.Popen(
            [
                str(root / "integrations/harbor/.venv/bin/harbor"),
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
            "coding-eval-import-harbor",
            "--job-directory",
            str(job),
            "--source-timezone",
            "UTC",
            "--output",
            str(output / "native-report.json"),
        ],
    )
    report = json.loads((output / "native-report.json").read_text())
    _check_matrix(report, job)
    hashes_before = {
        str(path.relative_to(job)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in job.rglob("*.json")
    }
    command = [
        "coding-eval-export",
        "--harbor-report",
        str(output / "native-report.json"),
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
    if hashes_before != after or readbacks[0]["experiment_id"] != readbacks[1]["experiment_id"]:
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
