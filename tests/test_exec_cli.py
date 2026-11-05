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
    assert (arguments.max_steps, arguments.max_tokens) == (100, 1_000_000)


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
