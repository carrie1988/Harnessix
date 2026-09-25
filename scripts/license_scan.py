"""按uv.lock逐项核对第三方许可证并产出可门禁的版本化报告。"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

POLICY_FILENAME = "governance/license-policy-v1.json"
REPORT_FILENAME = "governance/license-scan-v1.json"

_ALLOW_UNKNOWN = False


def _declared_license(name: str) -> str:
    """从已安装发行版元数据读取规范许可证表达式；缺失时返回UNKNOWN。"""

    try:
        meta = metadata(name)
    except PackageNotFoundError:
        return "NOT_INSTALLED"
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    classifiers = [
        value.split("::")[-1].strip()
        for value in meta.get_all("Classifier") or []
        if value.startswith("License")
    ]
    if classifiers:
        return " AND ".join(sorted(set(classifiers)))
    license_text = meta.get("License")
    if license_text:
        first = license_text.strip().splitlines()[0].strip()
        return first[:80] if first else "UNKNOWN"
    return "UNKNOWN"


def locked_names(lock_path: Path) -> list[str]:
    with lock_path.open("rb") as stream:
        document = tomllib.load(stream)
    names = [str(package["name"]) for package in document.get("package", [])]
    if not names:
        raise SystemExit("uv.lock缺少package清单")
    return sorted(set(names))


def build_report(lock_path: Path, policy: dict[str, object]) -> dict[str, object]:
    """逐项给出许可证判定；UNKNOWN与未命中白名单均为违规。"""

    allow = {str(item).upper() for item in policy.get("allow", [])}
    deny = {str(item).upper() for item in policy.get("deny", [])}
    self_packages = {str(item) for item in policy.get("self_packages", [])}
    overrides = {
        str(item["name"]): str(item["declared_license"])
        for item in policy.get("overrides", [])
        if isinstance(item, dict)
    }
    entries = []
    violations = []
    for name in locked_names(lock_path):
        if name in self_packages:
            entries.append({"name": name, "declared_license": "SELF", "status": "self"})
            continue
        declared = overrides.get(name) or _declared_license(name)
        normalized = declared.upper()
        parts = {part.strip() for part in normalized.split(" AND ")}
        status = "allow"
        if (
            normalized in deny
            or declared in {"UNKNOWN", "NOT_INSTALLED"}
            or not parts
            or any(part not in allow for part in parts)
        ):
            status = "violation"
            violations.append({"name": name, "declared_license": declared, "status": status})
        entries.append({"name": name, "declared_license": declared, "status": status})
    return {
        "spec_version": "harnessix.license-scan/v1",
        "lock_file": "uv.lock",
        "policy_file": POLICY_FILENAME,
        "package_count": len(entries),
        "violation_count": len(violations),
        "entries": entries,
        "violations": violations,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第三方许可证白名单扫描")
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--policy", type=Path, default=Path(POLICY_FILENAME))
    parser.add_argument("--output", type=Path, default=Path(REPORT_FILENAME))
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    policy = json.loads(arguments.policy.read_text(encoding="utf-8"))
    if policy.get("spec_version") != "harnessix.license-policy/v1":
        print("许可证策略版本不受支持", file=sys.stderr)
        return 1
    report = build_report(arguments.lock, policy)
    body = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if arguments.check:
        if not arguments.output.is_file() or arguments.output.read_bytes() != body:
            print("许可证报告漂移：请重新生成license-scan-v1.json", file=sys.stderr)
            return 1
        if report["violation_count"]:
            print(f"许可证违规：{report['violation_count']}项", file=sys.stderr)
            for item in report["violations"]:
                print(f"  {item['name']}: {item['declared_license']}", file=sys.stderr)
            return 1
        print(f"许可证扫描通过：{report['package_count']}个包全部命中白名单")
        return 0
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(body)
    print(f"许可证报告已生成：{arguments.output}（违规{report['violation_count']}项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
