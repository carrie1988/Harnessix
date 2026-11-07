"""构建Harnessix离线安装包。

内容：自带Python（python-build-standalone）、产品wheel与哈希锁定依赖。Python两种来源：
- --python-archive：install_only归档，按锁定摘要核对后解压；
- --python-dir：已安装的同类构建（如派生任务镜像里uv按其内置摘要核对后安装的托管Python），
  复制为私有副本后移除EXTERNALLY-MANAGED标记，收据记录版本与目录内容摘要。

包内顶层目录为harnessix/，HarnessixAgent在安装阶段解压到/opt，得到/opt/harnessix/bin/python。
依赖用`uv export --frozen --no-dev --no-emit-project --extra <provider>`导出的带哈希清单（须含所选
Provider的可选依赖，例如openai），经指定包镜像以
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


def _tree_digest(root: Path) -> str:
    """目录内容摘要：按相对路径排序，覆盖文件内容与链接目标。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            digest.update(b"L" + relative + b"\0" + str(path.readlink()).encode() + b"\0")
        elif path.is_file():
            digest.update(b"F" + relative + b"\0" + _sha256(path).encode() + b"\0")
    return digest.hexdigest()


def _python_tree(args: argparse.Namespace, work: Path) -> tuple[Path, dict]:
    root = work / "harnessix"
    if args.python_dir is not None:
        source = args.python_dir.resolve(strict=True)
        provenance = {"python_dir": str(source), "python_tree_sha256": _tree_digest(source)}
        shutil.copytree(source, root, symlinks=True)
        for marker in root.glob("lib/python3.*/EXTERNALLY-MANAGED"):
            marker.unlink()
        return root, provenance
    archive = _python_archive(args.python_archive, args.python_sha256, work)
    with tarfile.open(archive) as source:
        source.extractall(work, filter="data")
    (work / "python").rename(root)
    return root, {"python_archive": args.python_archive, "python_sha256": args.python_sha256}


def build(args: argparse.Namespace) -> dict:
    with tempfile.TemporaryDirectory(prefix="harnessix-bundle-") as temporary:
        work = Path(temporary)
        root, provenance = _python_tree(args, work)
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
        version = subprocess.run(
            [str(python), "-c", "import sys; print(sys.version.split()[0])"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(args.output, "w:gz") as bundle:
            bundle.add(root, arcname="harnessix")
    receipt = {
        "bundle": str(args.output),
        "bundle_sha256": _sha256(args.output),
        **provenance,
        "python_version": version,
        "wheel": args.wheel.name,
        "wheel_sha256": _sha256(args.wheel),
        "requirements_sha256": _sha256(args.requirements),
        "pip_index_url": args.pip_index_url,
    }
    args.output.with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--python-archive", help="install_only归档的HTTPS地址或本地路径")
    source.add_argument("--python-dir", type=Path, help="已安装的python-build-standalone目录")
    parser.add_argument("--python-sha256", help="--python-archive的锁定摘要")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True, help="带哈希的锁定依赖清单")
    parser.add_argument("--pip-index-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.python_archive is None) != (args.python_sha256 is None):
        parser.error("--python-archive与--python-sha256必须同时给出")
    print(json.dumps(build(args)))


if __name__ == "__main__":
    main()
