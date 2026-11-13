"""`harnessix code approvals`：查看、撤销这个工作区保存的“以后不再询问”命令规则（ADR 0128）。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from harnessix.product_config.command_rules import rule_label
from harnessix.product_config.private_json import serialized_update
from harnessix.product_ui.approval_rules import load_rules, native_shell, rules_path, save_rules
from harnessix.product_ui.cli import _default_state_directory, _workspace
from harnessix.product_ui.errors import ProductUIError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harnessix code approvals",
        description="查看或撤销这个工作区保存的“以后不再询问”命令规则",
    )
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--state-directory")
    parser.add_argument("--remove", type=int, metavar="序号", help="撤销列表中的第几条规则")
    return parser


def approvals_main(argv: Sequence[str]) -> None:
    arguments = _parser().parse_args(argv)
    try:
        workspace = _workspace(arguments.workspace)
    except ProductUIError as error:
        print(f"无法读取规则：{error.message}", file=sys.stderr)
        raise SystemExit(2) from None
    state_root = (
        Path(arguments.state_directory).absolute()
        if arguments.state_directory
        else _default_state_directory(workspace)
    )
    store, shell = rules_path(state_root), native_shell()
    if arguments.remove is None:
        print(rules_text(load_rules(store, shell)))
        return
    with serialized_update(store):
        rules = load_rules(store, shell)
        if not 1 <= arguments.remove <= len(rules):
            print(f"没有第 {arguments.remove} 条规则；当前共 {len(rules)} 条。", file=sys.stderr)
            raise SystemExit(2)
        removed = rules[arguments.remove - 1]
        save_rules(store, shell, rules[: arguments.remove - 1] + rules[arguments.remove :])
    print(f"已撤销：{rule_label(removed)}。以后这类命令会重新询问。")


def rules_text(rules: tuple[tuple[str, ...], ...]) -> str:
    builtin = "内置的只读命令（如列目录、查看工作区内的文件、git status）不询问，这一项不能撤销。"
    if not rules:
        return f"这个工作区没有保存“以后不再询问”的命令规则。\n{builtin}"
    lines = [f"这个工作区保存了 {len(rules)} 条“以后不再询问”的命令规则："]
    lines += [f"  {number}. {rule_label(prefix)}" for number, prefix in enumerate(rules, start=1)]
    lines += ["撤销某一条：harnessix code approvals --remove 序号", builtin]
    return "\n".join(lines)
