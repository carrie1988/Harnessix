"""从uv.lock生成确定性CycloneDX SBOM；--check用于逐字节漂移门禁。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

SBOM_VERSION = "harnessix.sbom/v1"
LOCK_FILENAME = "uv.lock"


def _lock_packages(lock_path: Path) -> list[dict[str, object]]:
    with lock_path.open("rb") as stream:
        document = tomllib.load(stream)
    packages = document.get("package")
    if not isinstance(packages, list) or not packages:
        raise SystemExit("uv.lock缺少package清单")
    return packages


def build_sbom(lock_path: Path) -> dict[str, object]:
    """以固定字段顺序构造SBOM对象；不包含时间戳、路径或本机信息。"""

    components: list[dict[str, object]] = []
    for package in _lock_packages(lock_path):
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise SystemExit("uv.lock包含缺少name/version的package")
        source = package.get("source")
        registry = (
            source.get("registry") if isinstance(source, dict) else None
        )
        hashes: list[dict[str, str]] = []
        sdist = package.get("sdist")
        if isinstance(sdist, dict) and isinstance(sdist.get("hash"), str):
            algorithm, _, digest = sdist["hash"].partition(":")
            hashes.append({"alg": algorithm.upper(), "content": digest})
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pypi/{name}@{version}",
                "scope": "required" if registry else "excluded",
                **({"hashes": hashes} if hashes else {}),
            }
        )
    components.sort(key=lambda item: (str(item["name"]), str(item["version"])))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:harnessix:sbom:uv-lock",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "harnessix",
                "version": "0.1.0",
            },
            "properties": [{"name": "harnessix:sbom_contract", "value": SBOM_VERSION}],
        },
        "components": components,
    }


def canonical_bytes(sbom: dict[str, object]) -> bytes:
    return (json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode(
        "utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或校验CycloneDX SBOM")
    parser.add_argument("--lock", type=Path, default=Path(LOCK_FILENAME))
    parser.add_argument("--output", type=Path, default=Path("dist/sbom.cyclonedx.json"))
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    body = canonical_bytes(build_sbom(arguments.lock))
    if arguments.check:
        if not arguments.output.is_file() or arguments.output.read_bytes() != body:
            print("SBOM漂移：dist/sbom.cyclonedx.json与uv.lock不一致", file=sys.stderr)
            return 1
        digest = hashlib.sha256(body).hexdigest()
        print(f"SBOM一致：{digest}")
        return 0
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(body)
    print(f"SBOM已生成：{arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
