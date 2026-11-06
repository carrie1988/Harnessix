"""构建Harnessix离线安装包。

内容：自带Python（python-build-standalone，摘要核对）、产品wheel与哈希锁定依赖。

包内顶层目录为harnessix/，HarnessixAgent在安装阶段解压到/opt，得到/opt/harnessix/bin/python。
依赖用`uv export --frozen --no-dev --no-emit-project`导出的带哈希清单，经指定包镜像以
--require-hashes安装，镜像无法替换内容。构建完成后在包内Python中导入SDK做冒烟检查，并写收据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _python_archive(source: str, expected: str, work: Path) -> Path:
    archive = work / "python.tar.gz"
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=600) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
    else:
        shutil.copyfile(source, archive)
    if _sha256(archive) != expected:
        raise SystemExit("Python归档摘要与锁定值不一致")
    return archive


def build(args: argparse.Namespace) -> dict:
    with tempfile.TemporaryDirectory(prefix="harnessix-bundle-") as temporary:
        work = Path(temporary)
        archive = _python_archive(args.python_archive, args.python_sha256, work)
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
        root = work / "harnessix"
        (work / "python").rename(root)
        python = root / "bin" / "python3"
        if not (root / "bin" / "python").exists():
            (root / "bin" / "python").symlink_to("python3")
        install = [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
        ]
        if args.pip_index_url:
            install += ["--index-url", args.pip_index_url]
        subprocess.run([*install, "--require-hashes", "-r", str(args.requirements)], check=True)
        subprocess.run([*install, "--no-deps", str(args.wheel)], check=True)
        subprocess.run([str(python), "-c", "import harnessix.sdk.agent_client"], check=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(args.output, "w:gz") as bundle:
            bundle.add(root, arcname="harnessix")
    receipt = {
        "bundle": str(args.output),
        "bundle_sha256": _sha256(args.output),
        "python_archive": args.python_archive,
        "python_sha256": args.python_sha256,
        "wheel": args.wheel.name,
        "wheel_sha256": _sha256(args.wheel),
        "requirements_sha256": _sha256(args.requirements),
        "pip_index_url": args.pip_index_url,
    }
    args.output.with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python-archive", required=True, help="install_only归档的HTTPS地址或本地路径"
    )
    parser.add_argument("--python-sha256", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True, help="带哈希的锁定依赖清单")
    parser.add_argument("--pip-index-url")
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(build(parser.parse_args())))


if __name__ == "__main__":
    main()
