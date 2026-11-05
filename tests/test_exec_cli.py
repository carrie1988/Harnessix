from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from harnessix.exec_cli import HeadlessConsole, NeedsHumanInput, _parser, approval_policy
from harnessix.protocol.contracts import PublicApprovalRequestContent


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
