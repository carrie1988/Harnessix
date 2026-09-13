"""产品错误自助的静态、脱敏目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ProductErrorHelp:
    """可安全展示的错误原因、影响和处理动作。"""

    code: str
    title: str
    cause: str
    impact: str
    action: str
    doc_anchor: str


_DIAGNOSTICS: Final = "docs/operations/diagnostics.md#9-常见错误分诊"
_HELP: Final = {
    "approval_stale": ProductErrorHelp(
        "approval_stale",
        "审批已经变化",
        "当前Turn、Call、Approval或安全指纹已不是打开窗口时的身份。",
        "本次决定没有发送，也没有分配新的命令身份。",
        "刷新会话并重新打开审批窗口。",
        _DIAGNOSTICS,
    ),
    "approval_evidence_required": ProductErrorHelp(
        "approval_evidence_required",
        "审批证据不完整",
        "Diff尚未完整读取，或者完整性校验未通过。",
        "批准操作被失败关闭；拒绝操作仍可使用。",
        "重试读取证据，确认服务端Artifact能力，或拒绝该操作。",
        _DIAGNOSTICS,
    ),
    "question_stale": ProductErrorHelp(
        "question_stale",
        "问题已经变化",
        "当前Turn不再等待窗口所绑定的问题。",
        "回答没有发送，也没有消费命令身份。",
        "刷新会话并处理最新问题。",
        _DIAGNOSTICS,
    ),
    "question_answer_invalid": ProductErrorHelp(
        "question_answer_invalid",
        "回答格式无效",
        "回答为空或超过协议长度上限。",
        "回答没有发送。",
        "输入1至4000个字符后重新提交。",
        _DIAGNOSTICS,
    ),
    "turn_control_stale": ProductErrorHelp(
        "turn_control_stale",
        "Turn控制已经失效",
        "Turn身份或状态已经变化，不再接受当前控制动作。",
        "Cancel或Steer命令没有发送。",
        "刷新会话并根据最新Turn状态继续。",
        _DIAGNOSTICS,
    ),
    "steering_invalid": ProductErrorHelp(
        "steering_invalid",
        "补充输入无效",
        "Steer正文为空或超过协议长度上限。",
        "补充输入没有发送。",
        "修正文后重新打开Steer窗口。",
        _DIAGNOSTICS,
    ),
    "diff_unavailable": ProductErrorHelp(
        "diff_unavailable",
        "Diff不可读取",
        "服务端未提供Artifact分页能力，或批量变更缺少完整Diff引用。",
        "涉及Diff的批准操作被禁用。",
        "升级或重新连接服务端，也可以拒绝当前操作。",
        _DIAGNOSTICS,
    ),
    "artifact_reference_changed": ProductErrorHelp(
        "artifact_reference_changed",
        "Artifact引用发生变化",
        "分页结果与审批绑定的Artifact身份不一致。",
        "证据被视为不可用，批准操作被禁用。",
        "刷新会话后重新读取；若重复出现，检查服务端Artifact存储。",
        _DIAGNOSTICS,
    ),
    "artifact_pagination_stalled": ProductErrorHelp(
        "artifact_pagination_stalled",
        "Artifact分页未推进",
        "服务端返回了重复或不连续的记录偏移。",
        "证据读取停止，批准操作被禁用。",
        "重新读取；若重复出现，检查服务端分页实现。",
        _DIAGNOSTICS,
    ),
    "artifact_integrity_failed": ProductErrorHelp(
        "artifact_integrity_failed",
        "Artifact完整性失败",
        "记录数、UTF-8字节数或SHA-256与公开引用不一致。",
        "不可信Diff不会用于批准。",
        "拒绝当前操作，并检查Artifact生成与读取链路。",
        _DIAGNOSTICS,
    ),
    "artifact_read_timeout": ProductErrorHelp(
        "artifact_read_timeout",
        "Artifact读取超时",
        "完整Diff未能在产品读取时限内完成。",
        "批准操作保持禁用，未产生领域命令。",
        "检查本地服务状态后重试，或拒绝当前操作。",
        _DIAGNOSTICS,
    ),
    "connection_failure": ProductErrorHelp(
        "connection_failure",
        "Agent连接不可用",
        "stdio服务关闭、握手失败或响应不符合公共协议。",
        "结果未知的命令不会被自动重放。",
        "使用Ctrl+R重连，再依据持久Replay确认结果。",
        _DIAGNOSTICS,
    ),
    "product_internal_failure": ProductErrorHelp(
        "product_internal_failure",
        "产品内部错误",
        "错误未命中可公开的稳定自助分类。",
        "不能据此判断业务命令成功或失败。",
        "重新打开产品并通过持久会话确认结果，再采集脱敏诊断。",
        _DIAGNOSTICS,
    ),
}
_CONNECTION_CODES: Final = frozenset(
    {
        "connection_not_ready",
        "connection_start_failed",
        "handshake_failed",
        "invalid_response",
        "server_closed",
        "server_start_failed",
    }
)


def product_error_help(code: str | None) -> ProductErrorHelp:
    """只返回静态条目，未知错误不回显输入码或原始异常。"""

    if code in _CONNECTION_CODES:
        return _HELP["connection_failure"]
    if code is not None and code in _HELP:
        return _HELP[code]
    return _HELP["product_internal_failure"]
