"""启动独立的Langfuse栈；私有凭据留在指定目录，不启动模型或删除卷。

无法直连Docker Hub的主机用pull-images经显式镜像源前缀拉取（不修改Docker全局配置），
或在可访问的机器上export-images后docker load；之后以--image-source local启动：
逐个核对本地镜像ID与锁中该平台的配置摘要，不从网络拉取。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import subprocess
from pathlib import Path

_PROJECT = "harnessix-langfuse-acceptance"
_LOCK = Path(__file__).with_name("images.lock.json")
_PLATFORMS = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "arm64": "linux/arm64"}
_PLATFORMS["aarch64"] = "linux/arm64"
_DOCKER_HUB_HOSTS = {"docker.io", "docker.langfuse.com"}
_MIRROR = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:[0-9]{1,5})?")


def _locked(target_platform: str) -> dict[str, tuple[str, str, str]]:
    """服务名 → (仓库, 该平台清单摘要, 该平台镜像ID)；均来自锁定的多架构索引。"""
    images = json.loads(_LOCK.read_text())["images"]
    result = {}
    for name, entry in images.items():
        locked = entry["platforms"][target_platform]
        repository = entry["registry_digest"].split("@", 1)[0]
        result[name] = (repository, locked["manifest_digest"], locked["image_id"])
    return result


def _local_tag(name: str, image_id: str) -> str:
    return f"harnessix-langfuse/{name}:{image_id.removeprefix('sha256:')[:12]}"


def _image_id(reference: str) -> str | None:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _pull_verified(name: str, reference: str, target_platform: str, image_id: str) -> str:
    """按清单摘要拉取，镜像ID必须等于锁中该平台的配置摘要，再打本地标签。"""
    subprocess.run(
        ["docker", "pull", "--quiet", "--platform", target_platform, reference], check=True
    )
    if _image_id(reference) != image_id:
        raise SystemExit(f"{name}镜像ID与锁不一致，停止")
    tag = _local_tag(name, image_id)
    subprocess.run(["docker", "tag", image_id, tag], check=True)
    return tag


def _docker_hub_path(repository: str) -> str | None:
    """Docker Hub上的仓库路径；其他仓库主机（如cgr.dev）返回None，直接拉取。"""
    first, separator, rest = repository.partition("/")
    if separator and ("." in first or ":" in first):
        return rest if first in _DOCKER_HUB_HOSTS else None
    return repository if separator else f"library/{repository}"


def pull_images(target_platform: str, mirror: str | None) -> list[str]:
    """Docker Hub镜像经显式镜像源前缀拉取；按摘要寻址，镜像源无法替换内容。"""
    if mirror is not None:
        mirror = mirror.removeprefix("https://").rstrip("/")
        if not _MIRROR.fullmatch(mirror):
            raise SystemExit("镜像源须为主机名或主机名:端口")
    tags = []
    for name, (repository, manifest, image_id) in _locked(target_platform).items():
        hub_path = _docker_hub_path(repository)
        source = f"{mirror}/{hub_path}" if mirror and hub_path else repository
        tags.append(_pull_verified(name, f"{source}@{manifest}", target_platform, image_id))
    return tags


def export_images(target_platform: str, output: Path) -> str:
    """按平台清单摘要拉取并核对镜像ID，打本地标签后保存为0600归档；返回归档摘要。"""
    tags = pull_images(target_platform, None)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as archive:
        subprocess.run(["docker", "save", *tags], stdout=archive, check=True)
    digest = hashlib.sha256()
    with output.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_image_override(directory: Path) -> Path:
    """核对本机已加载的锁定镜像，生成只用本地镜像、禁止拉取的Compose覆盖文件。"""
    host = _PLATFORMS[platform.machine().lower()]
    lines = ["services:"]
    for name, (_, _, image_id) in _locked(host).items():
        tag = _local_tag(name, image_id)
        if _image_id(tag) != image_id:
            raise SystemExit(f"本机缺少锁定的{name}镜像或镜像ID不一致；不从网络拉取")
        lines += [f"  {name}:", f"    image: {tag}", "    pull_policy: never"]
    path = directory / "compose.local-images.yaml"
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    return path


def prepare_credentials(directory: Path, port: int) -> Path:
    """首次独占创建0600凭据，后续复用；拒绝链接及公开目录，不输出密钥。"""

    if directory.is_symlink():
        raise ValueError("验收目录不能是链接")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("验收目录必须由当前用户独占且权限为0700")
    path = directory / "credentials.env"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("验收凭据权限无效")
        fields = dict(line.split("=", 1) for line in path.read_text().splitlines())
        if fields.get("LANGFUSE_PORT") != str(port):
            raise ValueError("端口与既有验收状态不一致")
        return path
    names = (
        "DATABASE_PASSWORD",
        "REDIS_AUTH",
        "CLICKHOUSE_PASSWORD",
        "MINIO_PASSWORD",
        "SALT",
        "ENCRYPTION_KEY",
        "NEXTAUTH_SECRET",
        "INIT_USER_PASSWORD",
    )
    values = {name: secrets.token_hex(32) for name in names}
    values.update(
        LANGFUSE_PUBLIC_KEY="pk-lf-" + secrets.token_hex(16),
        LANGFUSE_SECRET_KEY="sk-lf-" + secrets.token_hex(32),
        LANGFUSE_PORT=str(port),
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("".join(f"{name}={value}\n" for name, value in values.items()))
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("up", "stop", "pull-images", "export-images"))
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--port", type=int, default=3300)
    parser.add_argument("--image-source", choices=("registry", "local"), default="registry")
    parser.add_argument("--platform", choices=sorted(set(_PLATFORMS.values())))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mirror", help="显式镜像源主机，只用于Docker Hub镜像；不写入仓库")
    args = parser.parse_args()
    if args.action == "pull-images":
        target = args.platform or _PLATFORMS[platform.machine().lower()]
        print(json.dumps({"action": "pull-images", "tags": pull_images(target, args.mirror)}))
        return
    if args.action == "export-images":
        if not args.platform or not args.output:
            parser.error("export-images需要--platform与--output")
        digest = export_images(args.platform, args.output)
        print(json.dumps({"action": "export-images", "archive_sha256": digest}))
        return
    if args.state_directory is None:
        parser.error("up/stop需要--state-directory")
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须为1024到65535")
    if not (args.state_directory / "credentials.env").exists():
        volumes = subprocess.check_output(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={_PROJECT}",
                "-q",
            ],
            text=True,
        )
        if volumes.strip():
            raise SystemExit("已存在Langfuse数据卷；需恢复原凭据文件，不能生成替代状态。")
    credentials = prepare_credentials(args.state_directory, args.port)
    command = [
        "docker",
        "compose",
        "--project-name",
        _PROJECT,
        "--env-file",
        str(credentials),
        "--file",
        str(Path(__file__).with_name("compose.yaml")),
    ]
    if args.image_source == "local":
        command.extend(["--file", str(local_image_override(args.state_directory))])
    command.extend(["up", "-d"] if args.action == "up" else ["stop"])
    subprocess.run(command, check=True)
    print(json.dumps({"action": args.action, "credentials_file": str(credentials)}))


if __name__ == "__main__":
    main()
