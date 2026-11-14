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
# 读取、搜索与修改文件的失败说明已改为写给模型的英文（ADR 0129）；这里保留用户此前看到的原因，
# 界面只在收到英文说明时改用它，仍是中文的说明照原样截取首句。
_USER_REASONS = {
    "tool_anchor_not_found": "文件中没有该字面锚点；核对文件或改用窄范围grep定位",
    "tool_anchor_ambiguous": "字面锚点不唯一；使用更具体的锚点或grep返回的行号",
    "tool_anchor_outside_page": "文本页未覆盖锚点；减小lines_before或增大页容量",
    "tool_definition_unavailable": (
        "定义视图仅支持有界Python实际声明；请用text查看原文或核对anchor"
    ),
    "tool_limit_exceeded": (
        "扫描超过工具上限（条目、字节或深度），未返回结果；不要原样重试，请把path缩小到更具体的"
        "子目录，或先用glob定位候选文件再grep"
    ),
    "tool_path_denied": (
        "路径不可读：读工具只读工作区内的文件，路径相对工作区根（如src/app.py）；工作区以外的"
        "路径（如临时目录）只能用命令工具访问；部分敏感路径始终不可读"
    ),
    "tool_not_found": "路径不存在：先用glob或list_files确认相对工作区根的路径",
    "tool_timeout": (
        "读取或搜索超时，未返回结果；不要原样重试，请把path缩小到具体子目录、收窄include，"
        "或先用glob定位候选文件"
    ),
    "tool_expected_revision_required": (
        "list_files后续页必须携带上一成功结果的revision作为expected_revision"
    ),
    "tool_regex_invalid": (
        "表达式不是受支持的RE2模式或超过编译内存上限；请修正语法、移除反向引用/"
        "前后查找断言、简化表达式，或显式使用match_mode=literal查找字面量"
    ),
    "tool_no_files_searched": "没有文件匹配include，未搜索任何文件",
    **dict.fromkeys(
        (
            "tool_wrong_file_type",
            "tool_invalid_utf8",
            "tool_binary_file",
            "tool_offset_out_of_range",
            "tool_page_changed",
            "tool_workspace_changed",
            "tool_io_failed",
        ),
        "工作区读取未完成",
    ),
    **dict.fromkeys(
        ("artifact_not_found", "artifact_expired", "artifact_invalid_cursor"),
        "Artifact 读取未完成",
    ),
    "patch_context_not_found": "精确编辑锚点不存在",
    "patch_ambiguous_context": "编辑锚点不唯一，请提供更完整上下文",
    "patch_overlapping_edits": "编辑区间重叠",
    "patch_limit_exceeded": "Patch 完整后镜像超限",
    "patch_no_change": "Patch 未产生内容变化",
    "delivery_no_change": "Workspace事务包含无变化文件",
    "delivery_metadata_unsupported": "Windows事务不模拟POSIX可执行模式",
    "delivery_path_denied": "Workspace Patch路径不允许写入",
    "workspace_path_denied": "Workspace路径不允许访问",
    "workspace_parent_missing": "缺失资源的父目录不存在",
    "workspace_wrong_file_type": "路径中已有的父级不是目录，无法在其下创建文件",
    "workspace_patch_cwd_unsupported": "Workspace Patch只支持根级cwd",
    "workspace_platform_unsupported": "Workspace平台不受支持",
    "workspace_patch_precondition_failed": (
        "expected_sha256不匹配；请重新调用目标文件的read_file，"
        "仅使用digest_status=complete时的content_sha256，禁止猜测摘要"
    ),
    "workspace_patch_target_exists": "create目标已存在；请read_file后用edit或replace，或先delete",
    "workspace_patch_target_missing": "目标不是现有普通文件；新文件请用create",
    "workspace_patch_unsafe_full_replace": (
        "完整替换会删除现有文件超过四分之一；请改用唯一锚定的edit或显式delete"
    ),
    "workspace_patch_edit_source_invalid": "edit目标不是受支持的UTF-8文本",
    "workspace_patch_preparation_timeout": (
        "审批预览准备超时（工作区较大或磁盘繁忙）；本次Patch未执行，工作区没有变化，"
        "可重试或拆成更小的修改"
    ),
}


def tool_call_text(content: PublicToolCallContent) -> str | None:
    """返回调用说明；计划更新另有计划行展示，返回None。"""

    return describe_tool(content.tool, content.arguments)


def describe_tool(tool: str, arguments: Mapping[str, JsonValue]) -> str | None:
    """用一句话说明工具调用要做什么，供对话记录与审批窗口共用。"""

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
        return _patch_summary(_patch_paths(arguments))
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

    returncode = content.output.get("returncode") if isinstance(content.output, dict) else None
    if content.outcome == "succeeded":
        return "完成" if returncode is None else f"完成（退出码 {returncode}）"
    if content.error is None:
        return f"未完成（{content.outcome}）"
    code = content.error.code
    if code == "process_cancelled":
        # 用户取消了正在运行的命令：这是他要的结果，不是需要解释的失败。
        return "已取消"
    if code == "process_nonzero_exit" and type(returncode) is int:
        # 退出1（测试失败、发现差异）与退出2（参数或输入无效）要不同处理（Beta Issue #13）。
        return f"失败（退出码 {returncode}）"
    if code in _ERRORS:
        return f"失败：{_ERRORS[code]}"
    reason = _USER_REASONS.get(code)
    if reason is None or not content.error.message.isascii():
        reason = content.error.message.split("。", 1)[0]
    return f"失败：{_short(reason)}（{code}）"


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


def _patch_summary(paths: list[str]) -> str:
    """一两个文件全部列出；更多时只给数量和第一个路径，完整清单在审批窗口和差异中。"""

    if len(paths) <= 2:
        return "修改文件：" + _short(", ".join(paths) or "（无）")
    return f"修改 {len(paths)} 个文件：{_short(paths[0])} 等"


def _short(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    more = len(line) > _MAX_TEXT or "\n" in text.strip()
    return line[:_MAX_TEXT] + ("…" if more else "")
