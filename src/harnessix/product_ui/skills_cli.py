"""`harnessix skills list`：命令行入口，清单内容由产品配置层生成。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from harnessix.product_config.skill_diagnostics import list_product_skills


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harnessix skills", description="查看可用的 Skill")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="列出 Agent 启动时可用的 Skill")
    listing.add_argument("workspace", nargs="?", type=Path, default=Path.cwd())
    return parser


def skills_main(argv: Sequence[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    print("\n".join(list_product_skills(arguments.workspace)))
