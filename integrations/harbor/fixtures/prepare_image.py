"""显式构建离线运行的协议验收镜像；联网仅发生在依赖准备，不调用真实模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.build_directory
    directory.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[3]
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--extra",
            "openai",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(directory / "requirements.txt"),
        ],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(directory / "wheels")],
        cwd=root,
        check=True,
    )
    wheels = list((directory / "wheels").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("构建目录必须只含一个产品wheel")
    for name in ("Dockerfile", "provider.py", "configure.py", "entrypoint.sh"):
        shutil.copyfile(Path(__file__).with_name(name), directory / name)
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    inputs = hashlib.sha256(
        b"".join(
            (directory / name).read_bytes()
            for name in (
                "Dockerfile",
                "requirements.txt",
                "provider.py",
                "configure.py",
                "entrypoint.sh",
            )
        )
        + digest.encode()
    ).hexdigest()
    image = f"harnessix-harbor-protocol:{inputs[:16]}"
    subprocess.run(["docker", "build", "--tag", image, str(directory)], check=True)
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True
    ).strip()
    (directory / "image.json").write_text(
        json.dumps(
            {
                "image": image,
                "image_id": image_id,
                "wheel_sha256": digest,
                "build_inputs_sha256": inputs,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"image": image, "wheel_sha256": digest}))


if __name__ == "__main__":
    main()
