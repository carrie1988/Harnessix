"""沙箱外命令host_command（ADR 0113第2.2节）：逐条人工批准、必填理由、如实记为host_guarded。"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from harnessix.exec_cli import approval_policy
from harnessix.processes.supervisor_capabilities import probe_posix_process_capability
from harnessix.product_config.agent_context import _NATIVE_EXECUTION_INSTRUCTIONS
from harnessix.product_config.native_process import (
    attest_native_process_profile,
    native_process_binding,
    native_process_descriptor,
    native_process_executor_id,
)
from harnessix.product_config.process_command import decode_host_command
from harnessix.protocol.contracts import PublicApprovalRequestContent
from tests.product_config.test_native_process_action import _native_case, _result
from tests.product_config.test_native_process_profile import native_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="沙箱外命令只支持POSIX Process Owner")


def host_profile(**overrides: object):
    return native_profile(backend="host", profile_id="host-shell", **overrides)


def test_identity_schema_and_description() -> None:
    profile = host_profile()
    descriptor = native_process_descriptor(profile)
    assert descriptor.name == "host_command.host-shell"
    assert descriptor.version.startswith("harnessix.host-command/v1:")
    assert native_process_executor_id(profile) == "product.host-command.host-shell"
    assert native_process_binding(profile).executor_id == "product.host-command.host-shell"
    assert set(descriptor.input_schema["required"]) == {"profile", "cmd", "reason"}
    assert "需要用户批准" in descriptor.description and "没有隔离" in descriptor.description
    assert descriptor.requires_approval


@pytest.mark.parametrize(
    "arguments",
    [
        {"profile": "host-shell", "cmd": "pip install x"},
        {"profile": "host-shell", "cmd": "pip install x", "reason": "  "},
        {"profile": "host-shell", "cmd": "pip install x", "reason": "two\nlines"},
        {"profile": "host-shell", "cmd": "pip install x", "reason": "x" * 501},
        {"profile": "other", "cmd": "pip install x", "reason": "需要联网"},
    ],
)
def test_reason_is_required_and_bounded(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        decode_host_command("host-shell", arguments)  # type: ignore[arg-type]


def test_attestation_needs_no_boundary_assertion_and_records_host_guarded() -> None:
    reason, attested = attest_native_process_profile(
        host_profile(environment=()),
        probe_posix_process_capability(),
        environ={
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/dev",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "MODEL_API_KEY": "secret",
            "HARNESSIX_PRODUCT_CONFIG": "/x",
        },
        secret_names=("MODEL_API_KEY",),
    )
    assert reason == "verified" and attested is not None
    assert (attested.sandbox.level, attested.sandbox.backend, attested.sandbox.network) == (
        "host_guarded",
        "host",
        "full",
    )
    assert attested.native_sandbox is None
    # 沙箱外命令需要联网，继承代理变量；模型凭据与Harnessix变量永不继承。
    assert attested.environment["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "MODEL_API_KEY" not in attested.environment
    assert "HARNESSIX_PRODUCT_CONFIG" not in attested.environment


def test_headless_auto_mode_never_approves_host_commands() -> None:
    decide = approval_policy("auto")
    content = PublicApprovalRequestContent(
        approval_type="process",
        approval_id=UUID(int=1),
        call_id=UUID(int=2),
        request_fingerprint="a" * 64,
        policy_version="trusted-coding-action/v2",
    )
    assert decide(content, "host_command.host-shell") is None
    assert decide(content, "exec_command.shell") == "approved"


async def test_host_command_runs_only_after_a_human_approval(tmp_path: Path) -> None:
    profile = host_profile()
    async with _native_case(
        tmp_path,
        "echo outside > outside.txt",
        profile,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    ) as case:
        # 宿主不自动批准沙箱外命令：轮次停在等待审批。
        assert case.approval.decision is None
        assert case.approval.policy_id == "default.approve-canonical-effect"
        assert not (case.workspace / "outside.txt").exists()
        completed = await case.approve_and_resume()
        result = _result(completed)
        assert result.outcome == "succeeded"
        assert (case.workspace / "outside.txt").read_text() == "outside\n"
        plan = case.router.status(case.approval.plan_id).plan
        assert plan.binding.tool == "host_command.host-shell"
        assert {(item.kind, item.access) for item in plan.resources} == {
            ("process", "execute"),
            ("workspace", "write"),
            ("network", "connect"),
        }


def test_native_instructions_route_requested_commits_through_host_commands() -> None:
    assert "run git add and git commit through host_command" in _NATIVE_EXECUTION_INSTRUCTIONS
    assert "unless the user asks" in _NATIVE_EXECUTION_INSTRUCTIONS
    assert "Never push." in _NATIVE_EXECUTION_INSTRUCTIONS
