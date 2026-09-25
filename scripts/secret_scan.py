"""仓库与发行产物的Secret扫描门禁：固定规则集、零命中、正例自检。"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

SCAN_VERSION = "harnessix.secret-scan/v1"

# 固定规则集：私钥块、常见云/平台凭据与通用高熵赋值；版本化后规则变更必须升版本。
RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private_key_block", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("generic_api_assignment", re.compile(
        rb"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9/+_=-]{24,}['\"]"
    )),
    ("bearer_literal", re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~+/=-]{24,}")),
)

# 明确豁免：文档中的占位说明与公共示例不属于泄漏。
ALLOWLIST_PATHS = (
    ".env.example",
)

POSITIVE_FIXTURE = b'api_key = "AbCdEfGhIjKlMnOpQrStUvWx123456"'


def _tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ("git", "ls-files"),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return [root / name for name in completed.stdout.splitlines() if name.strip()]


def scan_paths(paths: Sequence[Path]) -> list[dict[str, object]]:
    """逐文件按固定规则集扫描；返回命中项（应为空）。"""

    findings: list[dict[str, object]] = []
    for path in paths:
        if any(str(path).endswith(skip) for skip in ALLOWLIST_PATHS):
            continue
        try:
            if not path.is_file() or path.is_symlink():
                continue
            body = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in body[:4096]:
            continue
        for rule_name, pattern in RULES:
            for match in pattern.finditer(body):
                line = body.count(b"\n", 0, match.start()) + 1
                findings.append(
                    {"rule": rule_name, "path": str(path), "line": line}
                )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仓库与产物Secret扫描")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.self_check:
        hits = [rule for rule, pattern in RULES if pattern.search(POSITIVE_FIXTURE)]
        if not hits:
            print("Secret扫描自检失败：正例夹具未被任何规则命中", file=sys.stderr)
            return 1
        print(f"Secret扫描自检通过：正例命中规则{hits}")
        return 0
    root = arguments.root.resolve()
    findings = scan_paths(_tracked_files(root))
    for extra in (root / "dist",):
        if extra.is_dir():
            findings.extend(scan_paths(sorted(extra.rglob("*"))))
    if findings:
        print(f"Secret扫描命中{len(findings)}处：", file=sys.stderr)
        for item in findings[:20]:
            print(f"  {item['rule']} {item['path']}:{item['line']}", file=sys.stderr)
        return 1
    print(f"Secret扫描通过（{SCAN_VERSION}）：仓库与产物零命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
