"""显式构建离线运行的协议验收镜像；联网仅发生在依赖准备，不调用真实模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def stage(directory: Path) -> str:
    """在有仓库与uv的机器上导出锁定依赖、构建产品wheel并复制夹具文件；返回wheel摘要。"""
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
    return hashlib.sha256(wheels[0].read_bytes()).hexdigest()


def build(
    directory: Path, base_image: str | None, base_image_id: str | None, pip_index: str | None
) -> None:
    """在目标主机构建；指定本地基础镜像时先核对其ID，镜像源只用于带哈希校验的pip依赖。"""
    wheels = list((directory / "wheels").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("构建目录必须只含一个产品wheel")
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    names = ("Dockerfile", "requirements.txt", "provider.py", "configure.py", "entrypoint.sh")
    inputs = hashlib.sha256(
        b"".join((directory / name).read_bytes() for name in names) + digest.encode()
    ).hexdigest()
    command = ["docker", "build", "--tag", f"harnessix-harbor-protocol:{inputs[:16]}"]
    if base_image is not None:
        actual = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", base_image], text=True
        ).strip()
        if actual != base_image_id:
            raise ValueError("本地基础镜像ID与锁定值不一致")
        command += ["--build-arg", f"BASE_IMAGE={base_image}"]
    if pip_index is not None:
        command += ["--build-arg", f"PIP_INDEX_URL={pip_index}"]
    image = command[3]
    subprocess.run([*command, str(directory)], check=True)
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
                "base_image": base_image,
                "base_image_id": base_image_id,
                "pip_index_url": pip_index,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"image": image, "wheel_sha256": digest}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-directory", type=Path, required=True)
    parser.add_argument("--stage-only", action="store_true", help="只生成构建目录，不构建镜像")
    parser.add_argument("--build-only", action="store_true", help="只用已生成的构建目录构建镜像")
    parser.add_argument("--base-image", help="已核对的本地基础镜像标签")
    parser.add_argument("--base-image-id")
    parser.add_argument("--pip-index-url")
    args = parser.parse_args()
    if (args.base_image is None) != (args.base_image_id is None):
        parser.error("--base-image与--base-image-id必须同时给出")
    if not args.build_only:
        stage(args.build_directory)
    if not args.stage_only:
        build(args.build_directory, args.base_image, args.base_image_id, args.pip_index_url)


if __name__ == "__main__":
    main()
