from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from harnessix.exec_cli import (
    HeadlessConsole,
    NeedsHumanInput,
    _commands,
    _parser,
    approval_policy,
)
from harnessix.product_ui.verification import verification_summary as _verification
from harnessix.protocol.contracts import PublicApprovalRequestContent, PublicItem


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

    def end_of_input(_prompt: str) -> str:
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
    assert _verification([]) == {"status": "no_changes"}
    tested = [
        _call(patch, "apply_patch_batch"),
        _result(patch, "succeeded"),
        _call(check, *run),
        _result(check, "succeeded"),
    ]
    assert _verification(tested) == {"status": "passed_after_last_edit", "last_check": "pytest -q"}
    edited_again = [
        *tested,
        _call(later_patch, "apply_patch_batch"),
        _result(later_patch, "succeeded"),
    ]
    assert _verification(edited_again) == {"status": "unverified"}
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
