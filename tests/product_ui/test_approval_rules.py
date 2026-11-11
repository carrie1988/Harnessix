"""沙箱外命令的免询问规则（ADR 0128）：只读命令、保存的前缀、符号链接复核与查看撤销。"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.product_ui import cli as product_cli
from harnessix.product_ui.approval_automation import AUTO_APPROVAL_REASON, ApprovalAutomation
from harnessix.product_ui.approval_rules import (
    READ_ONLY_REASON,
    CommandRules,
    load_rules,
    rules_path,
)
from harnessix.product_ui.interaction_screens import _remember_label
from harnessix.product_ui.interactions import ApprovalBinding, PendingApproval


def _pending(cmd: str, *, tool: str = "host_command.host-shell") -> PendingApproval:
    return PendingApproval(
        binding=ApprovalBinding(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64),
        approval_type="process",
        policy_version="v1",
        tool=tool,
        tool_version="1",
        effect_class="non_idempotent_write",
        arguments_json=json.dumps({"cmd": cmd, "reason": "需要在沙箱外运行"}),
        diff_artifact=None,
    )


@pytest.fixture
def rules(tmp_path: Path) -> CommandRules:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/app.py").write_text("print(1)\n", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return CommandRules(workspace, rules_path(tmp_path / "state"), shell="posix")


def test_read_only_commands_inside_the_workspace_need_no_question(rules: CommandRules) -> None:
    assert rules.reason("ls -la") == READ_ONLY_REASON
    assert rules.reason("cat src/app.py") == READ_ONLY_REASON
    assert rules.reason("git status") == READ_ONLY_REASON
    # 敏感文件、工作区外、复合命令和会修改的命令照旧询问。
    for cmd in ("cat .env", "cat /etc/passwd", "cat src/app.py | sh", "rm src/app.py", "pytest"):
        assert rules.reason(cmd) is None, cmd


@pytest.mark.skipif(os.name != "posix", reason="创建符号链接在Windows上需要额外权限")
def test_symlinks_cannot_lead_a_read_only_command_out_or_to_secrets(
    rules: CommandRules, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (rules.workspace / "notes.txt").symlink_to(outside)
    (rules.workspace / "settings").symlink_to(rules.workspace / ".env")
    (rules.workspace / "alias.py").symlink_to(rules.workspace / "src/app.py")

    assert rules.reason("cat notes.txt") is None
    assert rules.reason("cat settings") is None
    assert rules.reason("cat alias.py") == READ_ONLY_REASON


def test_a_remembered_prefix_is_saved_for_the_workspace_and_survives_restarts(
    rules: CommandRules,
) -> None:
    assert rules.reason("python -m unittest -v") is None
    prefix = rules.offer("python -m unittest -v")
    assert prefix == ("python", "-m", "unittest")

    rules.remember(prefix)
    rules.remember(prefix)

    reopened = CommandRules(rules.workspace, rules.store, shell="posix")
    matched = reopened.reason("python -m unittest discover")
    assert matched == "匹配你保存的规则：python -m unittest *"
    assert reopened.reason("python -m pytest") is None
    assert reopened.reason("python -m unittest; rm -rf .") is None
    assert load_rules(rules.store, "posix") == (prefix,)
    # 规则按Shell保存：PowerShell不会套用POSIX下保存的规则。
    assert load_rules(rules.store, "powershell") == ()
    if os.name == "posix":
        assert rules.store.stat().st_mode & 0o077 == 0


def test_a_damaged_rules_file_means_no_rules_not_a_crash(rules: CommandRules) -> None:
    rules.store.parent.mkdir(parents=True)
    rules.store.write_text("{not json", encoding="utf-8")
    assert rules.reason("python -m unittest") is None
    rules.store.write_text(json.dumps({"spec_version": "other", "rules": [1]}), encoding="utf-8")
    assert load_rules(rules.store, "posix") == ()


def test_automation_applies_rules_only_to_commands_outside_the_sandbox(
    rules: CommandRules,
) -> None:
    automation = ApprovalAutomation(command_rules=rules)

    assert automation.reason(_pending("ls")) == READ_ONLY_REASON
    assert automation.reason(_pending("ls", tool="exec_command.sandbox")) is None
    assert automation.reason(_pending("ls", tool="some_tool")) is None
    assert automation.reason(replace(_pending("ls"), approval_type="tool")) is None
    assert ApprovalAutomation().reason(_pending("ls")) is None


def test_remembering_saves_a_prefix_when_one_is_offered_and_the_exact_call_otherwise(
    rules: CommandRules,
) -> None:
    automation = ApprovalAutomation(command_rules=rules)
    tests = _pending("npm test -- --watch=false")
    assert automation.rule_offer(tests) == "npm test *"

    automation.remember(tests)

    assert automation.reason(_pending("npm test")) == "匹配你保存的规则：npm test *"
    assert load_rules(rules.store, "posix") == (("npm", "test"),)
    # 复合命令和危险命令没有可保存的前缀：只记住本会话里完全相同的调用。
    compound = _pending("npm test && npm run build")
    assert automation.rule_offer(compound) is None
    automation.remember(compound)
    assert automation.reason(compound) == AUTO_APPROVAL_REASON
    assert automation.reason(_pending("npm test && npm run deploy")) is None
    assert automation.rule_offer(_pending("rm -rf build")) is None
    assert load_rules(rules.store, "posix") == (("npm", "test"),)


def test_the_second_approve_button_says_what_will_be_remembered(rules: CommandRules) -> None:
    automation = ApprovalAutomation(command_rules=rules)
    command = _pending("python -m unittest")
    assert _remember_label(command, automation.rule_offer) == (
        "批准并以后不再询问：python -m unittest *"
    )
    assert _remember_label(_pending("rm -rf x"), automation.rule_offer) == (
        "批准并在本会话允许相同操作"
    )
    assert _remember_label(command, None) == "批准并在本会话允许相同操作"
    edit = replace(command, approval_type="patch")
    assert _remember_label(edit, automation.rule_offer) == "批准并自动接受本会话的修改"


def test_saved_rules_can_be_listed_and_revoked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    arguments = ["approvals", str(workspace), "--state-directory", str(state)]

    product_cli.code_main(arguments)
    assert "没有保存" in capsys.readouterr().out

    saved = CommandRules(workspace, rules_path(state))
    saved.remember(("python", "-m", "unittest"))
    saved.remember(("npm", "test"))
    product_cli.code_main(arguments)
    listed = capsys.readouterr().out
    assert "1. python -m unittest *" in listed and "2. npm test *" in listed

    product_cli.code_main([*arguments, "--remove", "1"])
    assert "已撤销：python -m unittest *" in capsys.readouterr().out
    assert load_rules(rules_path(state), saved.shell) == (("npm", "test"),)

    with pytest.raises(SystemExit) as missing:
        product_cli.code_main([*arguments, "--remove", "5"])
    assert missing.value.code == 2 and "没有第 5 条规则" in capsys.readouterr().err
