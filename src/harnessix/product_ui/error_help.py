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
_KEPT = "本轮已停止；之前完成的修改仍保留在工作区。"
_RESEND = "处理后重新发送任务即可继续。"
# 任务（Turn）以错误结束时F1显示的条目；按错误码静态选择，不回显模型服务返回的原文。
_TURN_HELP: Final = {
    entry.code: entry
    for entry in (
        ProductErrorHelp(
            "provider_authentication",
            "模型服务拒绝了 API Key",
            "API Key 填错、已过期，或没有使用该模型的权限。",
            "本轮没有执行任何操作。",
            "改正环境变量或配置中的 Key，用 harnessix code doctor --probe-model 核对，"
            "然后重新打开 Harnessix 再发送任务。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "provider_rate_limit",
            "模型服务限流",
            "请求太频繁，或账户的额度、并发达到上限。",
            _KEPT,
            "稍等片刻再发送任务；经常出现时检查服务商的额度和并发限制。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "provider_transport",
            "连不上模型服务",
            "网络不通、代理设置不对，或服务地址填错。",
            _KEPT,
            "检查网络和代理（HTTPS_PROXY 或配置里的代理地址），用 harnessix code doctor "
            "--probe-model 核对。" + _RESEND,
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "provider_internal",
            "模型服务内部错误",
            "模型服务暂时故障。",
            _KEPT,
            "稍后重新发送任务；持续出现时换用其他模型或联系服务商。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "provider_context_overflow",
            "对话超出模型的上下文长度",
            "会话累积的内容超过了模型能处理的长度。",
            _KEPT,
            "按 Ctrl+N 新建会话，简要说明之前的进展后继续。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "context_budget_exceeded",
            "会话内容放不进上下文窗口",
            "会话累积的历史、工具说明和必要指令，超过了配置的上下文窗口留给输入的部分；"
            "窗口较小、任务较长或读入了很大的文件时更容易遇到。",
            _KEPT,
            "模型支持更大的窗口时：按 Ctrl+Q 退出，重新运行 harnessix code configure 把 "
            "--context-window-tokens 调大（如 131072），再打开 Harnessix，选中这个会话发送“继续”。"
            "不能调大时，按 Ctrl+N 新建会话，简要说明之前的进展后继续。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "provider_invalid_provider_output",
            "模型返回的内容不完整或格式不对",
            "模型服务这一次的回复缺少结束标记或用量，或者工具调用的结构不合规；多为偶发。",
            _KEPT,
            "直接发送“继续”接着做；反复出现时换用其他模型，或用 harnessix code doctor "
            "--probe-model 核对模型服务。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "provider_max_output_tokens",
            "模型单次输出达到长度上限",
            "一次要生成的内容太长。",
            _KEPT,
            "让 Agent 分步完成，或把任务拆小后重新发送。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "budget_exceeded",
            "本轮 Token 用量达到上限",
            "任务较大，本轮用量超过了设定的上限。",
            _KEPT,
            "直接发送“继续”开始新一轮；需要时在配置里调高上限。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "time_budget_exceeded",
            "本轮运行时间达到上限",
            "任务较大，本轮耗时超过了设定的上限。",
            _KEPT,
            "直接发送“继续”开始新一轮；需要时在配置里调高上限。",
            _DIAGNOSTICS,
        ),
        ProductErrorHelp(
            "uncertain_effect",
            "操作结果无法确认",
            "某个修改或命令执行时被中断，或者结果没能确认。",
            "Harnessix 不会自动重做这一步，以免重复修改。",
            "先用 git status、git diff 查看工作区的实际改动，再告诉 Agent 当前情况并继续。",
            _DIAGNOSTICS,
        ),
    )
}
_TURN_HELP["provider_provider_internal"] = _TURN_HELP["provider_internal"]
_TURN_FAILED: Final = ProductErrorHelp(
    "turn_failed",
    "上一轮任务未完成",
    "任务以错误结束，对话记录里有具体说明。",
    "之前完成的修改仍保留在工作区，未完成的步骤不会自动重试。",
    "按对话记录中的说明处理后重新发送任务；需要排查时运行 harnessix code doctor。",
    _DIAGNOSTICS,
)
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


def current_error_help(
    notice_code: str | None, turn_status: str | None, turn_error_code: str | None
) -> ProductErrorHelp | None:
    """F1当前该显示的错误条目：先看连接等通知，再看本会话最近一轮的失败；都没有时返回None。"""

    if notice_code is not None:
        return product_error_help(notice_code)
    if turn_status in {"failed", "interrupted"} and turn_error_code is not None:
        return _TURN_HELP.get(turn_error_code, _TURN_FAILED)
    return None
