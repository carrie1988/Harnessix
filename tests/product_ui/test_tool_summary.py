"""对话记录用人话描述工具调用、结果和审批，不暴露内部标识或写给模型的提示。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.tool_rejections import rejection_result
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
        (
            _call("apply_patch_batch", files=[{"path": f"svc{i}/h.py"} for i in range(1, 9)]),
            "修改 8 个文件：svc1/h.py 等",
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


@pytest.mark.parametrize(
    ("code", "hint", "shown"),
    [
        (
            "process_nonzero_exit",
            "The command ran and exited with a non-zero status; this is not a launch failure.",
            "失败：命令以非零状态退出",
        ),
        (
            "process_timeout",
            "The command reached the time limit and was terminated, so it did not finish.",
            "失败：命令超时",
        ),
        (
            "tool_invalid_arguments",
            "A plan needs 1 to 8 steps; this one has 9.",
            "失败：参数无效，Agent 会调整后重试",
        ),
        # 目录拒绝的持久结果不变；给模型的英文反馈只在模型线协议视图里。
        (
            "unknown_tool",
            rejection_result(uuid4()).error.message,
            "失败：工具未注册（unknown_tool）",
        ),
    ],
)
def test_results_hide_model_facing_hints(code: str, hint: str, shown: str) -> None:
    failed = PublicToolResultContent(
        call_id=uuid4(),
        outcome="failed",
        error=PublicFailure(code=code, message=hint, retryable=False, category="tool"),
    )
    assert tool_result_text(failed) == shown


def test_successful_command_shows_its_exit_code() -> None:
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


def _failed(code: str, message: str) -> PublicToolResultContent:
    return PublicToolResultContent(
        call_id=uuid4(),
        outcome="failed",
        error=PublicFailure(code=code, message=message, retryable=False, category="tool"),
    )


@pytest.mark.parametrize(
    ("code", "model_message", "shown"),
    [
        (
            "tool_not_found",
            "Path not found. Paths are relative to the workspace root; confirm the path with "
            "glob or list_files before retrying",
            "失败：路径不存在：先用glob或list_files确认相对工作区根的路径（tool_not_found）",
        ),
        (
            "patch_ambiguous_context",
            "files[1].edits[1]: old_text matches more than one place (lines 1, 3); add the line "
            "before or after the place you mean",
            "失败：编辑锚点不唯一，请提供更完整上下文（patch_ambiguous_context）",
        ),
        (
            "workspace_wrong_file_type",
            "A path in files passes through an existing entry that is not a directory",
            "失败：路径中已有的父级不是目录，无法在其下创建文件（workspace_wrong_file_type）",
        ),
        (
            "workspace_parent_missing",
            "A parent directory of a path in files does not exist, and on this platform "
            "apply_patch_batch does not create directories",
            "失败：缺失资源的父目录不存在（workspace_parent_missing）",
        ),
        (
            "workspace_patch_precondition_failed",
            "files[0]: expected_sha256 does not match the current file",
            "失败：expected_sha256不匹配；请重新调用目标文件的read_file，"
            "仅使用digest_status=complete时的content_sha256，禁止猜测摘要"
            "（workspace_patch_precondition_failed）",
        ),
        # ADR 0129第2版在已定位的拒绝后追加整批未应用的说明：只写给模型，界面文案不变。
        (
            "patch_context_not_found",
            "files[1].edits[0]: old_text was not found in the file, and neither was its first "
            "non-blank line; read_file the part you want to change and copy old_text from the "
            "current text. Nothing was written: the batch is atomic, so the other file entry was "
            "not applied either.",
            "失败：精确编辑锚点不存在（patch_context_not_found）",
        ),
        (
            "workspace_path_denied",
            "A path in files is not a workspace path. Nothing was written.",
            "失败：Workspace路径不允许访问（workspace_path_denied）",
        ),
    ],
)
def test_english_model_diagnostics_keep_the_reason_users_saw_before(
    code: str, model_message: str, shown: str
) -> None:
    """读取、搜索与修改文件的失败说明改为写给模型的英文后，对话记录里的原因不变（ADR 0129）。"""
    assert tool_result_text(_failed(code, model_message)) == shown


def test_failures_that_are_still_chinese_or_unknown_are_shown_as_before() -> None:
    # 同一错误码的其他来源仍是中文说明时照原样截取首句，不套用上表。
    legacy = _failed("patch_limit_exceeded", "Patch 完整前镜像超限")
    assert tool_result_text(legacy) == "失败：Patch 完整前镜像超限（patch_limit_exceeded）"
    other = _failed("file_missing", "目标文件不存在。请先创建")
    assert tool_result_text(other) == "失败：目标文件不存在（file_missing）"


def test_every_english_read_failure_has_a_user_reason() -> None:
    """读工具每条写给模型的英文说明都有对应的用户文案，不会把英文诊断直接显示给用户。"""
    from harnessix.product_ui import tool_summary
    from harnessix.tools import runtime

    messages = {f"tool_{code}": text for code, text in runtime._READ_ERROR_MESSAGES.items()}
    messages |= runtime._ARTIFACT_ERROR_MESSAGES
    assert messages.keys() <= tool_summary._USER_REASONS.keys()
    for code, message in messages.items():
        assert message.isascii(), code
        assert message[:24] not in tool_result_text(_failed(code, message))
