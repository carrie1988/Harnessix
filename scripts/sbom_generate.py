"""从锁文件生成可离线校验的CycloneDX依赖清单，不推断实际安装集合。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote, urlsplit

from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource

if __package__ or __spec__ is not None:
    from scripts.cli_console import configure_utf8_console
else:
    from cli_console import configure_utf8_console


SBOM_VERSION = "harnessix.sbom/v2"
OUTPUT_FILENAME = "governance/sbom.cyclonedx.json"
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "governance/schemas/cyclonedx-1.5"


def _package_ref(package: dict) -> str:
    """Package URL同时作为图身份；当前锁文件的包名必须全局唯一。"""

    name, version = package.get("name"), package.get("version")
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        raise ValueError("锁文件包含无效包身份")
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    if normalized != name:
        raise ValueError("锁文件包名未规范化")
    return f"pkg:pypi/{quote(name, safe='')}@{quote(version, safe='')}"


def _archive_references(package: dict) -> list[dict]:
    """哈希归属于具体发行Archive，不能误称为已安装组件的内容摘要。"""

    archives = ([package["sdist"]] if "sdist" in package else []) + package.get("wheels", [])
    if not archives:
        raise ValueError("第三方包缺少发行Archive")
    references = []
    for archive in archives:
        url, digest = archive.get("url"), archive.get("hash")
        if not isinstance(url, str) or not isinstance(digest, str):
            raise ValueError("发行Archive缺少URL或摘要")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise ValueError("发行Archive来源或摘要不受支持")
        references.append(
            {
                "type": "distribution",
                "url": url,
                "hashes": [{"alg": "SHA-256", "content": digest.removeprefix("sha256:")}],
            }
        )
    return sorted(references, key=lambda item: item["url"])


def _dependency_refs(package: dict, identities: dict[str, str]) -> list[str]:
    """合并条件、可选与开发依赖的锁定全集；不声称某平台实际安装全部边。"""

    dependencies = list(package.get("dependencies", []))
    for key in ("optional-dependencies", "dev-dependencies"):
        for group in package.get(key, {}).values():
            dependencies.extend(group)
    try:
        return sorted({identities[item["name"]] for item in dependencies})
    except KeyError:
        raise ValueError("锁文件依赖图包含未解析身份") from None


def build_sbom(lock_path: Path, project_path: Path | None = None) -> dict:
    """固定锁文件与项目元数据，生成跨平台一致的pre-build库存而非运行时SBOM。"""

    project_path = project_path or lock_path.with_name("pyproject.toml")
    lock_body, project_body = lock_path.read_bytes(), project_path.read_bytes()
    packages = tomllib.loads(lock_body.decode())["package"]
    project = tomllib.loads(project_body.decode())["project"]
    identities = {item["name"]: _package_ref(item) for item in packages}
    if not packages or len(identities) != len(packages):
        raise ValueError("当前SBOM不支持空锁文件或同名多版本包")
    own = [item for item in packages if item["name"] == project["name"]]
    if (
        len(own) != 1
        or own[0]["version"] != project["version"]
        or own[0]["source"] != {"editable": "."}
    ):
        raise ValueError("项目元数据与锁文件根组件不一致")
    components = []
    for package in packages:
        if package is own[0]:
            continue
        if package.get("source") != {"registry": "https://pypi.org/simple"}:
            raise ValueError("第三方包来源未受审查")
        components.append(
            {
                "type": "library",
                "name": package["name"],
                "version": package["version"],
                "bom-ref": identities[package["name"]],
                "purl": identities[package["name"]],
                "externalReferences": _archive_references(package),
            }
        )
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "lifecycles": [{"phase": "pre-build"}],
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
                "bom-ref": identities[project["name"]],
                "purl": identities[project["name"]],
                "licenses": [{"expression": project["license"]}],
            },
            "properties": [
                {"name": "harnessix:sbom_contract", "value": SBOM_VERSION},
                {"name": "harnessix:inventory", "value": "locked-all-platforms-extras-dev"},
                {"name": "harnessix:lock_sha256", "value": hashlib.sha256(lock_body).hexdigest()},
                {
                    "name": "harnessix:project_sha256",
                    "value": hashlib.sha256(project_body).hexdigest(),
                },
            ],
        },
        "components": sorted(components, key=lambda item: (item["name"], item["version"])),
        "dependencies": sorted(
            (
                {"ref": identities[item["name"]], "dependsOn": _dependency_refs(item, identities)}
                for item in packages
            ),
            key=lambda item: item["ref"],
        ),
    }


def validate_sbom(sbom: dict) -> None:
    """只使用固定上游Schema与引用，验证时禁止动态下载。"""

    if sbom.get("specVersion") != "1.5":
        raise ValueError("SBOM声明的规范版本不受支持")
    schemas = [json.loads(path.read_bytes()) for path in sorted(SCHEMA_ROOT.glob("*.schema.json"))]
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    schema = next(
        item for item in schemas if item["$id"] == "http://cyclonedx.org/schema/bom-1.5.schema.json"
    )
    Draft7Validator(schema, registry=registry, format_checker=FormatChecker()).validate(sbom)


def canonical_bytes(sbom: dict) -> bytes:
    return (json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description="生成或校验锁定依赖CycloneDX SBOM")
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--output", type=Path, default=Path(OUTPUT_FILENAME))
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    sbom = build_sbom(arguments.lock, arguments.project)
    validate_sbom(sbom)
    body = canonical_bytes(sbom)
    if arguments.check:
        if not arguments.output.is_file() or arguments.output.read_bytes() != body:
            print("SBOM缺失或漂移：请重新生成版本化依赖库存", file=sys.stderr)
            return 1
        print(f"SBOM一致且Schema有效：{hashlib.sha256(body).hexdigest()}")
        return 0
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(body)
    print("SBOM已生成并通过离线Schema校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
