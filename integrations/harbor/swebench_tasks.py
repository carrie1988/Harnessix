"""Harbor SWE-bench任务的固定与离线准备；任务正文不进入版本库，只提交锁文件。

lock：在能访问GitHub的机器上，对按注册表引用检出的上游任务目录逐文件计算摘要。
prepare：在评测主机上核对上游文件，确认本机已有原始sweb.eval镜像，构建只增加离线条件的派生镜像，
再把派生镜像名写入任务配置。派生层只做四件事：放入与官方安装脚本相同版本的uv（PyPI wheel，
摘要核对），设置包与Python下载镜像，预装uv管理的Python，信任本机GitHub原始文件离线镜像的CA
（与系统根证书合并）；不改变仓库、测试或判分脚本。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

LOCK_VERSION = "harnessix.harbor-task-lock/v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(task: Path) -> dict[str, str]:
    return {
        path.relative_to(task).as_posix(): _sha256(path)
        for path in sorted(task.rglob("*"))
        if path.is_file()
    }


def lock(upstream: Path, refs: list[dict[str, Any]], output: Path) -> None:
    """记录注册表引用与逐文件摘要；同一锁文件之后用于核对评测主机上的副本。"""
    tasks = []
    for ref in sorted(refs, key=lambda r: r["name"]):
        task = upstream / ref["name"]
        tasks.append({**ref, "files": _files(task)})
    output.write_text(
        json.dumps({"spec_version": LOCK_VERSION, "tasks": tasks}, indent=2) + "\n",
        encoding="utf-8",
    )


def _verify(upstream: Path, task: dict[str, Any]) -> Path:
    directory = upstream / task["name"]
    if _files(directory) != task["files"]:
        raise SystemExit(f"{task['name']}的上游文件与锁不一致")
    return directory


def _parent_image(dockerfile: Path) -> str:
    images = [
        line.split()[1]
        for line in dockerfile.read_text(encoding="utf-8").splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    if len(images) != 1:
        raise SystemExit("任务Dockerfile必须只有一个FROM")
    return images[0]


def _image_id(reference: str) -> str | None:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _uv_binaries(environment: dict[str, Any], workdir: Path) -> None:
    """从包镜像下载uv wheel，按PyPI公布的摘要核对后取出uv与uvx。"""
    index = f"{environment['pypi_mirror'].rstrip('/')}/uv/"
    with urllib.request.urlopen(index, timeout=60) as response:
        page = response.read().decode()
    wheel = environment["uv_wheel"]
    marker = page.find(wheel["filename"])
    if marker < 0:
        raise SystemExit("包镜像没有锁定的uv wheel")
    href_start = page.rfind('href="', 0, marker) + len('href="')
    href = page[href_start : page.find('"', href_start)].split("#", 1)[0]
    link = urllib.request.urljoin(index, href)
    with urllib.request.urlopen(link, timeout=300) as response:
        body = response.read()
    if hashlib.sha256(body).hexdigest() != wheel["sha256"]:
        raise SystemExit("uv wheel摘要与PyPI公布值不一致")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for name in ("uv", "uvx"):
            member = next(m for m in archive.namelist() if m.endswith(f".data/scripts/{name}"))
            target = workdir / name
            target.write_bytes(archive.read(member))
            target.chmod(0o755)


def _dockerfile(parent: str, environment: dict[str, Any]) -> str:
    bundle = "/etc/harbor-offline/ca-bundle.pem"
    return "\n".join(
        [
            f"FROM {parent}",
            "WORKDIR /testbed",
            "COPY uv uvx /root/.local/bin/",
            "COPY raw-mirror-ca.pem /etc/harbor-offline/raw-mirror-ca.pem",
            "RUN cat /etc/ssl/certs/ca-certificates.crt /etc/harbor-offline/raw-mirror-ca.pem "
            f"> {bundle}",
            f"ENV REQUESTS_CA_BUNDLE={bundle}",
            f"ENV UV_INDEX_URL={environment['pypi_mirror']} "
            f"PIP_INDEX_URL={environment['pypi_mirror']} "
            f"UV_PYTHON_INSTALL_MIRROR={environment['python_mirror']}",
            f"RUN /root/.local/bin/uv python install {environment['managed_python']} "
            "&& mkdir -p /logs",
            "",
        ]
    )


def prepare(
    upstream: Path, lock_path: Path, environment_path: Path, mirror_ca: Path, output: Path
) -> dict:
    """生成可直接交给Harbor的任务目录与派生镜像，返回包含全部镜像ID的收据。"""
    task_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    if task_lock["spec_version"] != LOCK_VERSION:
        raise SystemExit("任务锁版本不受支持")
    sources = {task["name"]: _verify(upstream, task) for task in task_lock["tasks"]}
    output.mkdir(parents=True, exist_ok=False)
    recipe = hashlib.sha256(environment_path.read_bytes() + mirror_ca.read_bytes()).hexdigest()
    image_tag = f"{_sha256(lock_path)[:12]}-{recipe[:8]}"
    receipt: dict[str, Any] = {
        "task_lock_sha256": _sha256(lock_path),
        "environment_sha256": _sha256(environment_path),
        "raw_mirror_ca_sha256": _sha256(mirror_ca),
        "tasks": {},
    }
    with tempfile.TemporaryDirectory(prefix="harbor-env-") as temporary:
        context = Path(temporary)
        _uv_binaries(environment, context)
        shutil.copyfile(mirror_ca, context / "raw-mirror-ca.pem")
        for task in task_lock["tasks"]:
            source = sources[task["name"]]
            parent = _parent_image(source / "environment" / "Dockerfile")
            parent_id = _image_id(parent)
            if parent_id is None:
                raise SystemExit(f"本机缺少{parent}；请经镜像源前缀拉取后再准备")
            tag = f"harnessix-harbor/{task['name'].lower()}:{image_tag}"
            (context / "Dockerfile").write_text(_dockerfile(parent, environment))
            subprocess.run(
                ["docker", "build", "--quiet", "--tag", tag, str(context)],
                check=True,
                env={**os.environ, "DOCKER_BUILDKIT": "1"},
            )
            target = output / task["name"]
            shutil.copytree(source, target)
            toml = (target / "task.toml").read_text(encoding="utf-8")
            if "docker_image" in toml or "[environment]" not in toml:
                raise SystemExit("任务配置已含镜像字段或缺少[environment]节")
            (target / "task.toml").write_text(
                toml.replace("[environment]\n", f'[environment]\ndocker_image = "{tag}"\n', 1),
                encoding="utf-8",
            )
            receipt["tasks"][task["name"]] = {
                "parent_image": parent,
                "parent_image_id": parent_id,
                "image": tag,
                "image_id": _image_id(tag),
            }
    (output / "prepare-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    locker = commands.add_parser("lock")
    locker.add_argument("--upstream", type=Path, required=True)
    locker.add_argument("--refs", type=Path, required=True)
    locker.add_argument("--output", type=Path, required=True)
    preparer = commands.add_parser("prepare")
    preparer.add_argument("--upstream", type=Path, required=True)
    preparer.add_argument("--lock", type=Path, required=True)
    preparer.add_argument("--environment", type=Path, required=True)
    preparer.add_argument("--raw-mirror-ca", type=Path, required=True)
    preparer.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "lock":
        lock(args.upstream, json.loads(args.refs.read_text()), args.output)
    else:
        receipt = prepare(
            args.upstream, args.lock, args.environment, args.raw_mirror_ca, args.output
        )
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
