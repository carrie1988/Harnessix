"""启动独立的本机Langfuse验收栈；私有凭据留在指定目录，不启动模型或删除卷。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
from pathlib import Path

_PROJECT = "harnessix-langfuse-acceptance"


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
    parser.add_argument("action", choices=("up", "stop"))
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=3300)
    args = parser.parse_args()
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
    command.extend(["up", "-d"] if args.action == "up" else ["stop"])
    subprocess.run(command, check=True)
    print(json.dumps({"action": args.action, "credentials_file": str(credentials)}))


if __name__ == "__main__":
    main()
