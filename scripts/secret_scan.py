"""仓库与发行产物的有界Secret扫描；漏扫和异常不能作为零命中通过。"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

if __package__ or __spec__ is not None:
    from scripts.cli_console import configure_utf8_console
    from scripts.secret_scan_archives import archive_kind, archive_members
    from scripts.secret_scan_contracts import ScanBudget, ScanIncompleteError, ScanLimits
else:
    from cli_console import configure_utf8_console
    from secret_scan_archives import archive_kind, archive_members
    from secret_scan_contracts import ScanBudget, ScanIncompleteError, ScanLimits

SCAN_VERSION = "harnessix.secret-scan/v2"

# 固定规则集：私钥块、常见云/平台凭据与通用高熵赋值；版本化后规则变更必须升版本。
RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private_key_block", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "generic_api_assignment",
        re.compile(
            rb"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9/+_=-]{24,}['\"]"
        ),
    ),
    ("bearer_literal", re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~+/=-]{24,}")),
)


def _tracked_files(root: Path) -> list[Path]:
    """NUL分隔保证空格、中文、换行文件名均为一个真实输入。"""

    try:
        completed = subprocess.run(
            ("git", "ls-files", "-z"), cwd=root, capture_output=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError):
        raise ScanIncompleteError("scan_discovery_failed") from None
    paths = [root / os.fsdecode(name) for name in completed.stdout.split(b"\x00") if name]
    if len(paths) > ScanLimits().entries:
        raise ScanIncompleteError("scan_entry_limit")
    return paths


def _artifact_files(directory: Path, *, required: bool = False) -> list[Path]:
    """明确处理目录遍历错误；不使用会吞掉错误的glob扫描。"""

    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        if required:
            raise ScanIncompleteError("scan_input_unreadable") from None
        return []
    except OSError:
        raise ScanIncompleteError("scan_input_unreadable") from None
    if not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ScanIncompleteError("scan_input_unsupported")
    files: list[Path] = []

    def on_error(_: OSError) -> None:
        raise ScanIncompleteError("scan_discovery_failed") from None

    for folder, directories, names in os.walk(directory, onerror=on_error, followlinks=False):
        for name in directories:
            child = Path(folder) / name
            try:
                child_metadata = child.lstat()
            except OSError:
                raise ScanIncompleteError("scan_discovery_failed") from None
            if (
                stat.S_ISLNK(child_metadata.st_mode)
                or getattr(child_metadata, "st_file_attributes", 0) & 0x400
            ):
                raise ScanIncompleteError("scan_input_unsupported")
        files.extend(Path(folder) / name for name in names)
        if len(files) > ScanLimits().entries:
            raise ScanIncompleteError("scan_entry_limit")
    if required and not files:
        raise ScanIncompleteError("scan_artifact_missing")
    return sorted(files)


def _read_file(path: Path, budget: ScanBudget) -> bytes:
    """拒绝链接/设备并有界读取；扫描期间文件变化不算成功覆盖。"""

    budget.entry(member=False)
    try:
        for candidate in (path, *path.parents):
            if (
                candidate.is_symlink()
                or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise ScanIncompleteError("scan_input_unsupported")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
            raise ScanIncompleteError("scan_input_unsupported")
        if before.st_size > budget.limits.file_bytes:
            raise ScanIncompleteError("scan_file_limit")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ScanIncompleteError("scan_input_changed")
            body = stream.read(budget.limits.file_bytes + 1)
            after = os.fstat(stream.fileno())
        if len(body) > budget.limits.file_bytes:
            raise ScanIncompleteError("scan_file_limit")
        current = path.stat()
        if len(body) != after.st_size or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            raise ScanIncompleteError("scan_input_changed")
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ScanIncompleteError("scan_input_changed")
    except OSError:
        raise ScanIncompleteError("scan_input_unreadable") from None
    budget.consume(len(body))
    return body


def _scan_rules(
    body: bytes, path: str, budget: ScanBudget, findings: list[dict[str, object]]
) -> None:
    for rule_name, pattern in RULES:
        budget.checkpoint()
        for match in pattern.finditer(body):
            budget.hit()
            line = body.count(b"\n", 0, match.start()) + 1
            findings.append({"rule": rule_name, "path": path, "line": line})


def _scan_body(
    body: bytes,
    name: str,
    path: str,
    depth: int,
    budget: ScanBudget,
    findings: list[dict[str, object]],
) -> None:
    _scan_rules(body, path, budget, findings)
    # 二进制同样扫描；仅对明确BOM文本追加规范编码视图，不猜编码或忽略解码错误。
    for bom, encoding in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if body.startswith(bom):
            try:
                normalized = body.decode(encoding).encode("utf-8")
            except UnicodeError:
                raise ScanIncompleteError("scan_text_encoding_invalid") from None
            budget.consume(len(normalized))
            _scan_rules(normalized, path, budget, findings)
            break
    kind = archive_kind(body, name)
    if kind is None:
        return
    if depth >= budget.limits.archive_depth:
        raise ScanIncompleteError("scan_archive_depth_limit")
    for ordinal, (member_name, data) in enumerate(archive_members(body, name, kind, budget), 1):
        # 成员名可能包含凭据或控制字符；诊断位置使用稳定成员序号而非原名。
        _scan_body(data, member_name, f"{path}::member-{ordinal}", depth + 1, budget, findings)


def scan_paths(
    paths: Sequence[Path], *, limits: ScanLimits | None = None
) -> list[dict[str, object]]:
    """完整覆盖才返回命中列表；输入/预算/格式失败抛固定码而非空列表。"""

    budget = ScanBudget(limits or ScanLimits(), time.monotonic())
    findings: list[dict[str, object]] = []
    try:
        for path in paths:
            body = _read_file(path, budget)
            _scan_body(body, path.name, str(path), 0, budget, findings)
    except KeyboardInterrupt:
        raise ScanIncompleteError("scan_cancelled") from None
    return findings


def _self_check() -> bool:
    """逐规则正反例，防止只有一条通用规则仍能让扫描器自检通过。"""

    positives = {
        "private_key_block": b"-----BEGIN " + b"PRIVATE KEY-----",
        "aws_access_key": b"AKIA" + b"A" * 16,
        "github_token": b"ghp_" + b"A" * 24,
        "slack_token": b"xoxb-" + b"A" * 16,
        "generic_api_assignment": b'api_key = "' + b"A" * 32 + b'"',
        "bearer_literal": b"Bearer " + b"A" * 32,
    }
    negatives = {
        "private_key_block": b"-----BEGIN " + b"PUBLIC KEY-----",
        "aws_access_key": b"AKIA" + b"A" * 15,
        "github_token": b"ghp_" + b"A" * 19,
        "slack_token": b"xoxb-" + b"A" * 9,
        "generic_api_assignment": b'api_key = "' + b"A" * 23 + b'"',
        "bearer_literal": b"Bearer " + b"A" * 23,
    }
    return set(positives) == {name for name, _ in RULES} and all(
        pattern.search(positives[name]) is not None and pattern.search(negatives[name]) is None
        for name, pattern in RULES
    )


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_console()
    parser = argparse.ArgumentParser(description="仓库与发行物有界Secret扫描")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--artifact-dir", type=Path, help="额外发行物目录；默认扫描root/dist")
    parser.add_argument("--self-check", action="store_true")
    arguments = parser.parse_args(argv)
    if not _self_check():
        print("Secret扫描自检失败：固定规则正反例不完整", file=sys.stderr)
        return 2
    if arguments.self_check:
        print(f"Secret扫描自检通过：{len(RULES)}条固定规则逐项验证")
        return 0
    try:
        root = arguments.root.resolve()
        paths = list(
            dict.fromkeys(
                [
                    *_tracked_files(root),
                    *_artifact_files(
                        arguments.artifact_dir or root / "dist",
                        required=arguments.artifact_dir is not None,
                    ),
                ]
            )
        )
        findings = scan_paths(paths)
    except KeyboardInterrupt:
        print("Secret扫描未完成：scan_cancelled", file=sys.stderr)
        return 2
    except ScanIncompleteError as error:
        print(f"Secret扫描未完成：{error.code}", file=sys.stderr)
        return 2
    except OSError:
        print("Secret扫描未完成：scan_input_unreadable", file=sys.stderr)
        return 2
    if findings:
        print(f"Secret扫描命中{len(findings)}处：", file=sys.stderr)
        for item in findings[:20]:
            location = hashlib.sha256(
                str(item["path"]).encode("utf-8", errors="surrogateescape")
            ).hexdigest()[:16]
            print(f"  {item['rule']} location-sha256:{location}:{item['line']}", file=sys.stderr)
        return 1
    print(f"Secret扫描通过（{SCAN_VERSION}）：{len(paths)}个输入完整覆盖且固定规则零命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
