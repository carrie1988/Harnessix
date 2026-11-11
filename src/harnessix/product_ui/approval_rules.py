"""工作区的命令免询问规则（ADR 0128）：内置只读命令，加用户保存的“以后不再询问”前缀。

规则只对沙箱外命令（host_command.*）生效，保存在本机该工作区的状态目录里，不进入代码仓库；
`harnessix code approvals`可以查看和撤销。无人值守的exec不使用这些规则。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from harnessix.product_config.command_rules import (
    ShellFamily,
    allowed_relative_path,
    read_only_paths,
    rule_label,
    rule_matches,
    rule_prefix,
    simple_command,
)
from harnessix.product_config.private_json import write_private_json
from harnessix.product_ui.interactions import PendingApproval

_SPEC_VERSION: Final = "harnessix.approval-rules/v1"
_HOST_COMMAND_PREFIX: Final = "host_command."
MAX_RULES: Final = 200
READ_ONLY_REASON: Final = "只读命令，无需询问（内置规则）"


def rules_path(state_root: Path) -> Path:
    return state_root / "approval-rules.json"


def native_shell() -> ShellFamily:
    return "powershell" if os.name == "nt" else "posix"


def load_rules(store: Path, shell: ShellFamily) -> tuple[tuple[str, ...], ...]:
    """读取当前Shell的规则；文件缺失或损坏都按没有规则处理，回到逐条询问。"""

    try:
        document = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(document, dict) or document.get("spec_version") != _SPEC_VERSION:
        return ()
    entries = document.get("rules")
    if not isinstance(entries, list):
        return ()
    rules: list[tuple[str, ...]] = []
    for entry in entries[:MAX_RULES]:
        prefix = entry.get("prefix") if isinstance(entry, dict) else None
        if (
            isinstance(prefix, list)
            and 1 <= len(prefix) <= 3
            and all(isinstance(word, str) and word for word in prefix)
            and entry.get("shell") == shell
        ):
            rules.append(tuple(prefix))
    return tuple(dict.fromkeys(rules))


def save_rules(store: Path, shell: ShellFamily, rules: tuple[tuple[str, ...], ...]) -> None:
    entries = [{"prefix": list(prefix), "shell": shell} for prefix in rules[:MAX_RULES]]
    write_private_json(store, {"spec_version": _SPEC_VERSION, "rules": entries})


def host_command(pending: PendingApproval) -> str | None:
    """沙箱外命令审批里的命令文本；其他审批返回None。"""

    if pending.approval_type != "process" or not pending.tool.startswith(_HOST_COMMAND_PREFIX):
        return None
    try:
        arguments = json.loads(pending.arguments_json)
    except ValueError:
        return None
    cmd = arguments.get("cmd") if isinstance(arguments, dict) else None
    return cmd if isinstance(cmd, str) else None


@dataclass(slots=True)
class CommandRules:
    """一个工作区的免询问判定：先看内置只读命令，再看用户保存的前缀。"""

    workspace: Path
    store: Path
    shell: ShellFamily = field(default_factory=native_shell)

    def reason(self, cmd: str) -> str | None:
        """可以不询问时返回写入审批记录的原因；否则返回None。"""

        words = simple_command(cmd, self.shell)
        if words is None:
            return None
        paths = read_only_paths(words, self.shell)
        if paths is not None and all(self._stays_inside(path) for path in paths):
            return READ_ONLY_REASON
        for prefix in load_rules(self.store, self.shell):
            if rule_matches(prefix, words, self.shell):
                return f"匹配你保存的规则：{rule_label(prefix)}"
        return None

    def offer(self, cmd: str) -> tuple[str, ...] | None:
        """批准这条命令时可以一并保存的前缀；复合命令与危险命令没有。"""

        words = simple_command(cmd, self.shell)
        return None if words is None else rule_prefix(words, self.shell)

    def remember(self, prefix: tuple[str, ...]) -> None:
        rules = load_rules(self.store, self.shell)
        if prefix not in rules:
            save_rules(self.store, self.shell, (*rules, prefix))

    def _stays_inside(self, path: str) -> bool:
        """路径解析符号链接后仍在工作区内，且不落到敏感文件上；文本判断看不到符号链接。"""

        if any(ch in "*?[]" for ch in path):
            return True  # 只有列出名称的命令允许通配符
        try:
            root = Path(os.path.realpath(self.workspace))
            relative = Path(os.path.realpath(root / path)).relative_to(root)
        except (OSError, ValueError):
            return False
        return allowed_relative_path(relative.as_posix())
