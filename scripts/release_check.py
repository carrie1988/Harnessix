"""发布工作流的检查：版本标签与包版本一致、CHANGELOG已写明本版本、构建目录只有本版本Wheel，并提取发布说明。

用法：
  release_check.py gate --tag v0.10.0b1        # 校验并把version、prerelease写入GITHUB_OUTPUT
  release_check.py wheel --dist dist --version 0.10.0b1   # 核对Wheel并把sha256写入GITHUB_OUTPUT
  release_check.py notes --version 0.10.0b1 --output notes.md
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
UNRELEASED_MARK = "未发布"


class ReleaseCheckError(Exception):
    """发布前提不满足；消息直接展示给发布者。"""


def project_version(pyproject: Path) -> str:
    return str(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"])


def checked_version(tag: str, version: str) -> Version:
    """标签必须是`v`加包版本原文，且版本符合PEP 440规范形式。"""

    if tag != f"v{version}":
        raise ReleaseCheckError(f"标签{tag}与pyproject.toml中的版本{version}不一致，应为v{version}")
    try:
        parsed = Version(version)
    except InvalidVersion:
        raise ReleaseCheckError(f"版本号{version}不符合PEP 440") from None
    if str(parsed) != version:
        raise ReleaseCheckError(f"版本号{version}不是规范形式，应写作{parsed}")
    return parsed


def release_notes(changelog: str, version: str) -> str:
    """返回`## <版本>`小节正文；小节缺失、为空或仍标为未发布时拒绝。"""

    heading = re.compile(rf"^## {re.escape(version)}(?:（[^）\n]*）)?[ \t]*$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ReleaseCheckError(f"CHANGELOG.md缺少“## {version}”小节")
    if UNRELEASED_MARK in match.group(0):
        raise ReleaseCheckError(f"CHANGELOG.md中{version}仍标为未发布，请改为发布日期")
    following = re.search(r"^## ", changelog[match.end() :], re.MULTILINE)
    end = match.end() + following.start() if following else len(changelog)
    body = changelog[match.end() : end].strip()
    if not body:
        raise ReleaseCheckError(f"CHANGELOG.md中{version}小节为空")
    return body + "\n"


def release_wheel(dist: Path, version: str) -> Path:
    """构建目录必须恰好是本版本的一个Wheel；构建工具写入的隐藏文件（如uv的.gitignore）不计入。"""

    expected = f"harnessix-{version}-py3-none-any.whl"
    found = sorted(item.name for item in dist.iterdir() if not item.name.startswith("."))
    if found != [expected]:
        raise ReleaseCheckError(f"{dist}中应只有{expected}，实际为{found}")
    return dist / expected


def _write_outputs(values: dict[str, str]) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as output:
            output.write(lines)
    sys.stdout.write(lines)


def _gate(tag: str) -> None:
    version = project_version(ROOT / "pyproject.toml")
    parsed = checked_version(tag, version)
    release_notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    _write_outputs({"version": version, "prerelease": str(parsed.is_prerelease).lower()})


def _wheel(dist: Path, version: str) -> None:
    digest = hashlib.sha256(release_wheel(dist, version).read_bytes()).hexdigest()
    _write_outputs({"sha256": digest})


def _notes(version: str, output: Path) -> None:
    notes = release_notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    output.write_text(notes, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布前置检查")
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("gate", help="校验标签、版本与CHANGELOG")
    gate.add_argument("--tag", required=True)
    wheel = commands.add_parser("wheel", help="核对构建出的Wheel并记录摘要")
    wheel.add_argument("--dist", required=True, type=Path)
    wheel.add_argument("--version", required=True)
    notes = commands.add_parser("notes", help="提取本版本的发布说明")
    notes.add_argument("--version", required=True)
    notes.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "gate":
            _gate(arguments.tag)
        elif arguments.command == "wheel":
            _wheel(arguments.dist, arguments.version)
        else:
            _notes(arguments.version, arguments.output)
    except ReleaseCheckError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
