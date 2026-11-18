from __future__ import annotations

import asyncio
import io
import json
from uuid import uuid4

import pytest

from harnessix.exec_cli import (
    HeadlessConsole,
    NeedsHumanInput,
    _commands,
    _parser,
    _stdout_is_terminal,
    _write_summary,
    approval_policy,
)
from harnessix.product_ui.verification import verification_summary as _verification
from harnessix.protocol.contracts import PublicApprovalRequestContent, PublicItem


def _status(summary: dict) -> dict:
    """判定字段；运行过的命令列表（commands_after_last_edit）另有用例核对。"""
    return {
        k: v
        for k, v in summary.items()
        if k not in {"commands_after_last_edit", "commands_omitted"}
    }


def _request(approval_type: str) -> PublicApprovalRequestContent:
    return PublicApprovalRequestContent.model_validate(
        {
            "approval_type": approval_type,
            "approval_id": uuid4(),
            "call_id": uuid4(),
            "request_fingerprint": "0" * 64,
            "policy_version": "test",
        }
    )


@pytest.mark.parametrize(
    ("mode", "approval_type", "tool", "expected"),
    [
        ("manual", "patch_batch", "apply_patch_batch", None),
        ("manual", "process", "exec_command.workspace", None),
        ("auto-edit", "patch_batch", "apply_patch_batch", "approved"),
        ("auto-edit", "patch", "apply_patch", "approved"),
        ("auto-edit", "process", "exec_command.workspace", None),
        ("auto", "process", "exec_command.workspace", "approved"),
        ("auto", "process", "run_profile.public-tests", "approved"),
        ("auto", "process", None, None),
        ("auto", "tool", "git_commit", None),
    ],
)
def test_approval_modes_only_auto_approve_edits_and_sandboxed_processes(
    mode, approval_type, tool, expected
):
    assert approval_policy(mode)(_request(approval_type), tool) == expected


def test_non_interactive_console_turns_a_human_prompt_into_needs_input():
    console = HeadlessConsole(quiet=True, interactive=False)
    with pytest.raises(NeedsHumanInput):
        asyncio.run(console.prompt("批准该操作？[y/N] "))


def test_an_interactive_prompt_that_reads_end_of_input_needs_a_human_too(monkeypatch):
    """标准输入是终端但读到结尾时，与非交互相同（Beta Issue #18）。

    此前EOFError冒出，进程退出码1且没有摘要。
    """

    def end_of_input(*_prompt: str) -> str:
        raise EOFError("EOF when reading a line")

    monkeypatch.setattr("builtins.input", end_of_input)
    console = HeadlessConsole(quiet=True, interactive=True)
    with pytest.raises(NeedsHumanInput) as needs:
        asyncio.run(console.prompt("批准该操作？[y/N] "))
    assert needs.value.__cause__ is None and needs.value.__suppress_context__


def test_decisions_typed_at_the_prompt_are_recorded_apart_from_policy_ones():
    """策略做的决定记harnessix-exec:<模式>；人在提示处输入的另记来源（Beta Issue #23）。

    此前都记harnessix-exec:auto，审计里分不出手工批准与自动批准。没有策略的普通CLI不变。
    """
    from harnessix.agent_cli import _actors, _typed_decision

    assert _actors("harnessix-exec:auto", approval_policy("auto")) == (
        "harnessix-exec:auto",
        "harnessix-exec:auto:interactive",
    )
    assert _actors("local-user", None) == ("local-user", "local-user")
    assert [_typed_decision(reply) for reply in ("y", "yes", "n", "")] == [
        "approved",
        "approved",
        "rejected",
        "rejected",
    ]
    with pytest.raises(ValueError):
        _actors("x" * 250, approval_policy("manual"))


def test_exec_requires_exactly_one_task_source():
    with pytest.raises(SystemExit):
        _parser().parse_args([])
    with pytest.raises(SystemExit):
        _parser().parse_args(["--prompt", "a", "--prompt-file", "b"])
    arguments = _parser().parse_args(["--prompt", "fix the bug"])
    assert arguments.approval_mode == "manual"
    # 未指定时由活动Profile链的计价决定默认预算。
    assert (arguments.max_steps, arguments.max_tokens) == (None, None)


def _item(content: dict[str, object]) -> PublicItem:
    return PublicItem.model_validate(
        {"item_id": uuid4(), "status": "completed", "content": content}
    )


def test_summary_lists_sandbox_commands_with_their_results_in_call_order():
    first, second, read = uuid4(), uuid4(), uuid4()
    call = {"kind": "tool_call", "tool_version": "v1", "requires_approval": True}
    items = [
        _item({**call, "call_id": read, "tool": "read_file", "effect_class": "read_only"}),
        _item(
            {
                **call,
                "call_id": first,
                "tool": "exec_command.testbed",
                "effect_class": "non_idempotent_write",
                "arguments": {"profile": "testbed", "cmd": "python -m pytest tests/a.py"},
            }
        ),
        _item(
            {
                **call,
                "call_id": second,
                "tool": "exec_command.testbed",
                "effect_class": "non_idempotent_write",
                "arguments": {"profile": "testbed", "cmd": "python repro.py"},
            }
        ),
        _item(
            {
                "kind": "tool_result",
                "call_id": first,
                "outcome": "failed",
                "output": {"returncode": 1},
            }
        ),
    ]

    assert _commands(items) == [
        {
            "tool": "exec_command.testbed",
            "cmd": "python -m pytest tests/a.py",
            "outcome": "failed",
            "returncode": 1,
        },
        {
            "tool": "exec_command.testbed",
            "cmd": "python repro.py",
            "outcome": None,
            "returncode": None,
        },
    ]


def test_local_free_chains_get_a_larger_token_budget(tmp_path):
    from harnessix.product_config.codec import canonical_product_config_bytes
    from harnessix.product_config.product_contracts import ConfigurationDraft
    from harnessix.product_config.wizard import build_product_config
    from harnessix.product_ui.cli import _chain_pricing
    from harnessix.product_ui.turn_budget import turn_budget_for

    def config(base_url: str):
        return build_product_config(
            ConfigurationDraft(provider_kind="openai_chat", base_url=base_url, model="m")
        )

    local, remote = config("http://127.0.0.1:18002/v1"), config("https://api.example.com/v1")
    assert local.chain_pricing() == "free" and remote.chain_pricing() == "unknown"
    path = tmp_path / "config.json"
    path.write_bytes(canonical_product_config_bytes(local))
    path.chmod(0o600)  # 产品配置必须是私有文件
    assert _chain_pricing(path, None) == "free"
    assert _chain_pricing(tmp_path / "missing.json", None) == "unknown"
    assert turn_budget_for("free").max_tokens == 6_000_000
    assert turn_budget_for("unknown").max_tokens == 3_000_000
    assert turn_budget_for("free").max_steps == turn_budget_for("unknown").max_steps == 100


def _call(call_id, tool, arguments=None):
    return _item(
        {
            "kind": "tool_call",
            "call_id": call_id,
            "tool": tool,
            "tool_version": "v1",
            "effect_class": "non_idempotent_write",
            "requires_approval": True,
            "arguments": arguments or {},
        }
    )


def _result(call_id, outcome):
    return _item({"kind": "tool_result", "call_id": call_id, "outcome": outcome})


def test_verification_counts_only_checks_after_the_last_successful_edit():
    patch, check, later_patch, failed_patch = uuid4(), uuid4(), uuid4(), uuid4()
    run = ("exec_command.testbed", {"profile": "testbed", "cmd": "pytest -q"})
    assert _status(_verification([])) == {"status": "no_changes"}
    tested = [
        _call(patch, "apply_patch_batch"),
        _result(patch, "succeeded"),
        _call(check, *run),
        _result(check, "succeeded"),
    ]
    assert _status(_verification(tested)) == {
        "status": "passed_after_last_edit",
        "last_check": "pytest -q",
    }
    edited_again = [
        *tested,
        _call(later_patch, "apply_patch_batch"),
        _result(later_patch, "succeeded"),
    ]
    assert _status(_verification(edited_again)) == {"status": "unverified"}
    # 失败的补丁不算改动；失败的检查如实报告。
    rejected = [
        *edited_again[:4],
        _call(failed_patch, "apply_patch_batch"),
        _result(failed_patch, "failed"),
    ]
    assert _verification(rejected)["status"] == "passed_after_last_edit"
    failing = [
        _call(patch, "apply_patch_batch"),
        _result(patch, "succeeded"),
        _call(check, *run),
        _result(check, "failed"),
    ]
    assert _verification(failing)["status"] == "failed_after_last_edit"


def _items(*items: PublicItem) -> dict[object, PublicItem]:
    return {item.item_id: item for item in items}


def test_the_approval_prompt_shows_the_exact_command_and_reason():
    """Beta Issue #26：此前提示只有“批准该操作？[y/N]”，--quiet下看不到将在沙箱外执行的命令。"""
    from harnessix.agent_cli import approval_prompt

    request = _request("process")
    command = 'Write-Output "APPROVAL-FIXTURE-B38"\n' + "x" * 300
    call = _call(
        request.call_id,
        "host_command.host-shell",
        {"cmd": command, "reason": "按任务要求运行一次", "timeout_seconds": 30},
    )
    text = approval_prompt(_items(_call(uuid4(), "read_file", {"path": "a"}), call), request)
    assert text.splitlines() == [
        "需要你批准：host_command.host-shell",
        '  命令：Write-Output "APPROVAL-FIXTURE-B38"',
        "    " + "x" * 300,
        "  理由：按任务要求运行一次",
        '  参数：{"timeout_seconds": 30}',
        "批准该操作？[y/N] ",
    ]


def test_the_approval_prompt_cannot_hide_the_command_behind_control_characters():
    from harnessix.agent_cli import approval_prompt

    request = _request("process")
    call = _call(
        request.call_id,
        "host_command.host-shell",
        {"cmd": "rm -rf x\r\x1b[2Kls\u202e", "reason": "r"},
    )
    assert "  命令：rm -rf x\\u000d\\u001b[2Kls\\u202e" in approval_prompt(_items(call), request)


def test_the_approval_prompt_lists_the_files_of_an_edit_without_their_contents():
    from harnessix.agent_cli import approval_prompt

    request = _request("patch_batch")
    files = [{"path": "src/a.py", "content": "secret body"}, {"path": "b.md", "content": "x"}]
    text = approval_prompt(
        _items(_call(request.call_id, "apply_patch_batch", {"files": files})), request
    )
    assert "  文件：src/a.py、b.md" in text and "secret body" not in text
    assert approval_prompt({}, request).endswith("批准该操作？[y/N] ")


def test_prompts_go_to_stderr_even_when_quiet(monkeypatch, capsys):
    """stdout只留给JSON摘要；--quiet只省略过程输出，审批提示照常显示。"""
    asked: list[tuple[object, ...]] = []
    monkeypatch.setattr("builtins.input", lambda *prompt: asked.append(prompt) or "y")
    console = HeadlessConsole(quiet=True, interactive=True)
    assert asyncio.run(console.prompt("需要你批准：x\n批准该操作？[y/N] ")) == "y"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "需要你批准：x\n批准该操作？[y/N] "
    assert asked == [()]


def test_a_check_whose_last_native_program_failed_is_not_a_pass():
    """Beta Issue #27：测试失败后又打印了一行，整条命令退出0，此前标成“修改后检查通过”。"""
    from harnessix.product_config.native_shells import NATIVE_EXIT_NOTE

    patch, check = uuid4(), uuid4()
    stderr = f"2 failed, 17 passed\n{NATIVE_EXIT_NOTE}1\n"
    preview = {"stdout": {"text": "TESTEXIT=1\n"}, "stderr": {"text": stderr}}
    items = [
        _call(patch, "apply_patch_batch"),
        _result(patch, "succeeded"),
        _call(check, "host_command.host-shell", {"cmd": "npm test; Write-Host done"}),
        _item(
            {
                "kind": "tool_result",
                "call_id": check,
                "outcome": "succeeded",
                "output": {"returncode": 0, "diagnostic_preview": preview},
            }
        ),
    ]
    assert _status(_verification(items)) == {
        "status": "failed_after_last_edit",
        "last_check": "npm test; Write-Host done",
        "failed_check": "npm test; Write-Host done",
        "last_native_returncode": 1,
    }
    # Beta Issue #36：之后一条成功的清理命令不覆盖这次失败，退出码仍来自失败的检查。
    cleanup = uuid4()
    cleaned = [
        *items,
        _call(cleanup, "host_command.host-shell", {"cmd": "Remove-Item tmp.txt"}),
        _result(cleanup, "succeeded"),
    ]
    assert _status(_verification(cleaned)) == {
        "status": "failed_after_last_edit",
        "last_check": "Remove-Item tmp.txt",
        "failed_check": "npm test; Write-Host done",
        "last_native_returncode": 1,
    }


class _Stream(io.StringIO):
    def __init__(self, *, terminal: bool) -> None:
        super().__init__()
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal


_CHINESE_SUMMARY = {"status": "completed", "final": "已修复“解析器”"}


@pytest.mark.parametrize("terminal", [False, True])
def test_the_summary_on_stdout_is_ascii_only_unless_it_is_a_terminal(monkeypatch, terminal):
    """重定向或管道可能被宿主转码，Windows PowerShell 5.1的`>`按系统代码页重写，中文吞掉引号后
    JSON无法解析（Beta Issue #26）。不是终端时只写ASCII，解析出的内容不变；终端里照常显示中文。
    """
    stream = _Stream(terminal=terminal)
    monkeypatch.setattr("sys.stdout", stream)
    _write_summary(_CHINESE_SUMMARY, None)
    text = stream.getvalue()
    assert json.loads(text) == _CHINESE_SUMMARY
    assert text.isascii() is not terminal


def test_a_closed_stdout_counts_as_not_a_terminal(monkeypatch):
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr("sys.stdout", stream)
    assert not _stdout_is_terminal()


def test_the_summary_file_keeps_utf8_text(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdout", _Stream(terminal=False))
    target = tmp_path / "summary.json"
    _write_summary(_CHINESE_SUMMARY, str(target))
    text = target.read_text(encoding="utf-8")
    assert "已修复“解析器”" in text and json.loads(text) == _CHINESE_SUMMARY
