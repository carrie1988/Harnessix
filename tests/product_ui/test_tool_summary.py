"""对话记录用人话描述工具调用、结果和审批，不暴露内部标识或写给模型的提示。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.product_ui.tool_summary import approval_text, tool_call_text, tool_result_text
from harnessix.protocol.contracts import (
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicFailure,
    PublicToolCallContent,
    PublicToolResultContent,
)


def _call(tool: str, **arguments: object) -> PublicToolCallContent:
    return PublicToolCallContent(
        call_id=uuid4(),
        tool=tool,
        tool_version="harnessix.native-command/v2:7861fe42004dff141e9f9ec1",
        effect_class="non_idempotent_write",
        arguments=arguments,  # type: ignore[arg-type]
        requires_approval=True,
    )


@pytest.mark.parametrize(
    ("call", "text"),
    [
        (
            _call("exec_command.shell", cmd="python3 -m unittest\necho done"),
            "运行（沙箱内）：python3 -m unittest…",
        ),
        (
            _call("host_command.host-shell", cmd="git commit -m fix"),
            "运行（沙箱外）：git commit -m fix",
        ),
        (
            _call("apply_patch_batch", files=[{"path": "a.py"}, {"path": "b/c.py"}]),
            "修改文件：a.py, b/c.py",
        ),
        (_call("read_file", path="src/app.py"), "读取 src/app.py"),
        (_call("grep", query="def main"), "搜索 “def main”"),
        (_call("git_review"), "查看 Git 状态和差异"),
        (_call("mcp.github.create_issue", title="x"), "调用 mcp.github.create_issue"),
    ],
)
def test_calls_read_as_what_the_agent_does(call: PublicToolCallContent, text: str) -> None:
    assert tool_call_text(call) == text
    assert "7861fe42" not in text


def test_plan_updates_are_left_to_the_plan_lines() -> None:
    assert tool_call_text(_call("update_plan", steps=[])) is None


def test_results_hide_model_facing_hints() -> None:
    hint = "检查已运行并以非零退出，不是启动失败。diagnostic_preview已完整展示stdout/stderr诊断正文"
    failed = PublicToolResultContent(
        call_id=uuid4(),
        outcome="failed",
        error=PublicFailure(
            code="process_nonzero_exit", message=hint, retryable=False, category="tool"
        ),
    )
    assert tool_result_text(failed) == "失败：命令以非零状态退出"
    passed = PublicToolResultContent(
        call_id=uuid4(), outcome="succeeded", output={"returncode": 0, "state": "exited"}
    )
    assert tool_result_text(passed) == "完成（退出码 0）"


def _approval(actor: str | None, outcome: str = "approved", reason: str | None = None):
    decision = (
        None
        if actor is None
        else PublicApprovalDecision(outcome=outcome, actor=actor, reason=reason)  # type: ignore[arg-type]
    )
    return PublicApprovalRequestContent(
        approval_type="process",
        approval_id=uuid4(),
        call_id=uuid4(),
        request_fingerprint="b" * 64,
        policy_version="trusted-coding-action/v2",
        decision=decision,
    )


def test_sandbox_auto_approvals_are_hidden_and_user_decisions_are_explained() -> None:
    assert approval_text(_approval("harnessix:sandbox-policy")) is None
    assert approval_text(_approval(None)) == "等待你审批（Ctrl+A）"
    assert approval_text(_approval("harnessix-code-local-user")) == "已批准"
    rejected = _approval("harnessix-code-local-user", "rejected", "不要借用别人的身份")
    assert approval_text(rejected) == "已拒绝：不要借用别人的身份"
