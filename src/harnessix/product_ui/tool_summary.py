"""把工具调用、结果和审批记录转成用户能直接看懂的一行说明。

内部工具名、版本、效果分类和写给模型的诊断提示不进入对话记录；审计细节仍在会话数据与导出中。
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from harnessix.agent.approvals import SYSTEM_APPROVAL_ACTOR_PREFIX
from harnessix.protocol.contracts import (
    PublicApprovalRequestContent,
    PublicToolCallContent,
    PublicToolResultContent,
)

_MAX_TEXT = 160
_FIXED = {
    "git_status": "查看 Git 状态",
    "git_diff": "查看 Git 差异",
    "git_review": "查看 Git 状态和差异",
    "rollback_workspace_patch": "撤销之前的一次修改",
}
_ERRORS = {
    "process_nonzero_exit": "命令以非零状态退出",
    "process_timeout": "命令超时",
    "approval_rejected": "已拒绝",
    "tool_invalid_arguments": "参数无效，Agent 会调整后重试",
}


def tool_call_text(content: PublicToolCallContent) -> str | None:
    """返回调用说明；计划更新另有计划行展示，返回None。"""

    tool, arguments = content.tool, content.arguments
    if tool == "update_plan":
        return None
    if tool in _FIXED:
        return _FIXED[tool]
    if tool.startswith(("exec_command.", "host_command.")):
        where = "沙箱内" if tool.startswith("exec_command.") else "沙箱外"
        return f"运行（{where}）：{_short(_text(arguments, 'cmd', 'command'))}"
    if tool.startswith("run_profile."):
        return f"运行检查：{tool.removeprefix('run_profile.')}"
    if tool == "apply_patch_batch":
        return "修改文件：" + _short(", ".join(_patch_paths(arguments)) or "（无）")
    if tool == "read_file":
        return f"读取 {_text(arguments, 'path')}"
    if tool == "grep":
        return f"搜索 “{_short(_text(arguments, 'query', 'pattern'))}”"
    if tool in {"glob", "list_files"}:
        return f"查找文件 {_text(arguments, 'pattern', 'path') or '.'}"
    if tool.startswith("skill."):
        return f"读取 Skill {_text(arguments, 'name', 'skill')}"
    return f"调用 {tool}"


def tool_result_text(content: PublicToolResultContent) -> str:
    """成功只说完成（命令附退出码）；失败给出简短原因，不转述写给模型的提示。"""

    if content.outcome == "succeeded":
        code = content.output.get("returncode") if isinstance(content.output, dict) else None
        return "完成" if code is None else f"完成（退出码 {code}）"
    if content.error is None:
        return f"未完成（{content.outcome}）"
    code = content.error.code
    if code in _ERRORS:
        return f"失败：{_ERRORS[code]}"
    first = content.error.message.split("。", 1)[0]
    return f"失败：{_short(first)}（{code}）"


def approval_text(content: PublicApprovalRequestContent) -> str | None:
    """系统策略自动放行的审批不展示；其余说明谁在何时做了什么决定。"""

    decision = content.decision
    if decision is None:
        return "等待你审批（Ctrl+A）"
    if decision.actor.startswith(SYSTEM_APPROVAL_ACTOR_PREFIX):
        return None
    verb = "已批准" if decision.outcome == "approved" else "已拒绝"
    return verb if not decision.reason else f"{verb}：{_short(decision.reason)}"


def _text(arguments: Mapping[str, JsonValue], *keys: str) -> str:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _patch_paths(arguments: Mapping[str, JsonValue]) -> list[str]:
    files = arguments.get("files")
    if not isinstance(files, list):
        return []
    return [str(item["path"]) for item in files if isinstance(item, dict) and "path" in item]


def _short(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    more = len(line) > _MAX_TEXT or "\n" in text.strip()
    return line[:_MAX_TEXT] + ("…" if more else "")
