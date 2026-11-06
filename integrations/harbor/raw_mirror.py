"""SWE-bench判分所需GitHub原始文件的只读离线镜像（只依赖标准库，collect除外）。

Harbor的SWE-bench判分脚本用swebench 4.0.3计算测试规格时，会从raw.githubusercontent.com下载
部分仓库的依赖清单；评测主机无法访问GitHub时判分会崩溃，并被记成0分。
- collect：在能访问GitHub的机器上，以同版本swebench对每个任务的tests/config.json计算测试规格，
  记录实际成功请求的原始文件，写入缓存目录和逐文件摘要锁；
- ca：在评测主机生成私有CA与该主机名证书，CA只加入派生镜像，供判分脚本信任；
- serve：只监听显式地址，只提供锁中摘要一致的文件，其余一律404，不转发任何请求。
评测主机上该主机名的443端口可能已被其他服务占用，因此镜像作为附加服务运行在每个试次自己的
Compose网络内（compose/raw-github-mirror.yaml），以网络别名提供该主机名，不占用宿主机端口。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

HOST = "raw.githubusercontent.com"
_PREFIX = f"https://{HOST}/"
LOCK_VERSION = "harnessix.raw-github-mirror-lock/v1"


def collect(tasks: Path, output: Path) -> dict[str, str]:
    """需在装有swebench==4.0.3的环境中运行；按判分脚本同样的调用记录全部成功下载。"""
    import requests  # noqa: PLC0415 - 只在采集环境中可用
    from swebench.harness.test_spec.test_spec import make_test_spec  # noqa: PLC0415

    fetched: dict[str, bytes] = {}
    original = requests.get

    def recording(url: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = original(url, *args, **kwargs)
        if url.startswith(_PREFIX) and response.status_code == 200:
            fetched[url.removeprefix(_PREFIX)] = response.content
        return response

    requests.get = recording
    for config in sorted(tasks.glob("*/tests/config.json")):
        make_test_spec(json.loads(config.read_text(encoding="utf-8")))
    output.mkdir(parents=True, exist_ok=False)
    files = {}
    for path, body in sorted(fetched.items()):
        target = output / "files" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        files[path] = hashlib.sha256(body).hexdigest()
    lock = {"spec_version": LOCK_VERSION, "host": HOST, "files": files}
    (output / "raw-mirror.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    return files


def load(cache: Path, lock_path: Path) -> dict[str, bytes]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("spec_version") != LOCK_VERSION or lock.get("host") != HOST:
        raise SystemExit("离线镜像锁版本或主机名不受支持")
    files = {}
    for path, digest in lock["files"].items():
        body = (cache / "files" / path).read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise SystemExit(f"离线镜像文件摘要不一致：{path}")
        files[path] = body
    return files


def certificate_authority(directory: Path) -> Path:
    """生成私有CA和HOST的服务端证书（openssl）；已存在时不覆盖，返回CA证书路径。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    ca_key, ca, key, request, cert = (
        directory / name for name in ("ca.key", "ca.pem", "server.key", "server.csr", "server.pem")
    )
    if ca.exists():
        return ca
    extensions = directory / "server.ext"
    extensions.write_text(f"subjectAltName=DNS:{HOST}\nextendedKeyUsage=serverAuth\n")
    commands = [
        ["req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650"]
        + ["-keyout", str(ca_key), "-out", str(ca)]
        + ["-subj", "/CN=harnessix eval offline mirror CA"],
        ["req", "-newkey", "rsa:3072", "-nodes", "-keyout", str(key), "-out", str(request)]
        + ["-subj", f"/CN={HOST}"],
        ["x509", "-req", "-in", str(request), "-CA", str(ca), "-CAkey", str(ca_key)]
        + ["-CAcreateserial", "-days", "3650", "-out", str(cert), "-extfile", str(extensions)],
    ]
    for command in commands:
        subprocess.run(["openssl", *command], check=True, capture_output=True)
    for secret in (ca_key, key):
        secret.chmod(0o600)
    return ca


def serve(files: dict[str, bytes], host: str, port: int, tls: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server接口
            body = files.get(unquote(self.path.split("?", 1)[0].lstrip("/")))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls / "server.pem", tls / "server.key")
    server = ThreadingHTTPServer((host, port), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    collector = commands.add_parser("collect")
    collector.add_argument("--tasks", type=Path, required=True)
    collector.add_argument("--output", type=Path, required=True)
    authority = commands.add_parser("ca")
    authority.add_argument("--directory", type=Path, required=True)
    server = commands.add_parser("serve")
    server.add_argument("--cache", type=Path, required=True)
    server.add_argument("--lock", type=Path, required=True)
    server.add_argument("--tls", type=Path, required=True)
    server.add_argument("--listen", required=True, help="显式监听地址，如Docker网桥IP")
    server.add_argument("--port", type=int, default=443)
    server.add_argument(
        "--container", action="store_true", help="在独立容器网络内运行，允许通配地址"
    )
    args = parser.parse_args()
    if args.command == "collect":
        print(json.dumps(collect(args.tasks, args.output), indent=2))
    elif args.command == "ca":
        print(certificate_authority(args.directory))
    else:
        if args.listen in {"0.0.0.0", "::", ""} and not args.container:
            raise SystemExit("离线镜像不能监听通配地址")
        serve(load(args.cache, args.lock), args.listen, args.port, args.tls)


if __name__ == "__main__":
    main()
