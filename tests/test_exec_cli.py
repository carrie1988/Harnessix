from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from harnessix.exec_cli import HeadlessConsole, NeedsHumanInput, _commands, _parser, approval_policy
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
    assert turn_budget_for("free").max_tokens == 2_000_000
    assert turn_budget_for("unknown").max_tokens == 1_000_000
    assert turn_budget_for("free").max_steps == turn_budget_for("unknown").max_steps == 100
