"""用已构建镜像生成五类真实产品协议任务；只准备文件，不执行模型。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-receipt", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    receipt = json.loads(args.image_receipt.read_text())
    for index, mode in enumerate(("pass", "task-fail", "approval", "deadline", "provider-fail"), 1):
        task = args.output_directory / f"{index:02d}-protocol-{mode}"
        task.mkdir(parents=True, exist_ok=False)
        (task / "tests").mkdir()
        (task / "environment").mkdir()
        # Harbor默认策略使用可变sidecar网络；本离线夹具显式固定内核none命名空间。
        (task / "environment/docker-compose.yaml").write_text(
            "services:\n  main:\n    network_mode: none\n"
        )
        config = f"""schema_version = "1.4"
artifacts = ["/logs/agent/harnessix-summary.json", "/app"]
[metadata]
purpose = "native-product-protocol-acceptance"
[environment]
docker_image = {json.dumps(receipt["image"])}
network_mode = "no-network"
workdir = "/app"
cpus = 1
memory_mb = 512
[agent]
timeout_sec = 90
[verifier]
timeout_sec = 30
environment_mode = "separate"
network_mode = "no-network"
"""
        (task / "task.toml").write_text(config)
        (task / "image-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (task / "instruction.md").write_text(
            f"This is a deterministic protocol integration fixture, mode={mode}.\n"
            "Use read_file on exercise.txt and answer REVIEW:<value> without any extra text.\n"
        )
        shutil.copyfile(Path(__file__).with_name("verify.py"), task / "tests/verify.py")
        (task / "tests/test.sh").write_text("#!/bin/sh\nset -eu\npython /tests/verify.py\n")
    print(
        json.dumps({"tasks": 5, "directory": str(args.output_directory), "image": receipt["image"]})
    )


if __name__ == "__main__":
    main()
