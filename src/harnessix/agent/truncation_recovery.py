"""不可用回复的恢复：回复没有工具调用、又不能当作答复时，宿主补发提示继续，而不是让Turn失败。

两种情况（ADR 0109第3、6版），显式开启后生效（产品默认开启）：

- 截断：回复在输出上限处中断。评测中模型偶尔在一步里写上万字的推导，用满输出上限后整题作废；
  思考模型也可能把输出额度全部用在推理上、正文一个字没有。
- 空回复：回复正常结束，却没有正文也没有工具调用。思考模型偶尔只推理、不作答就结束，
  往往发生在任务已经做完、只差一句总结的时候。

处理相同：

- 已有的正文照常作为助手消息落账（用量照常记账），本步不执行任何东西；
- 宿主以用户消息补发提示，说明发生了什么、要求继续，按Steering语义重入下一步；
- 连续最多补发两次（两种情况合并计数）；第三次，或步骤与Token预算已用尽时，仍按原错误失败。

被剩余预算压低的输出上限造成的截断仍归为预算耗尽（budget_capped_truncation），不在此恢复。
判定只读本Turn已持久化的补发Item，补发Item身份由Turn与步骤派生，重放确定；不新增事件类型。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid5

from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, Turn

# 身份串沿用第3版的名字：已持久化的补发Item由它派生，改名会使重放得到不同的Item。
TRUNCATION_RECOVERY_VERSION = "harnessix.truncation-recovery/v1"
MAX_CONSECUTIVE_RECOVERY_NOTICES = 2

RecoverableReply = Literal["truncated", "empty"]

TRUNCATED_REPLY_NOTICE = (
    "[Harnessix host notice] Your last response reached the output token limit and was cut off "
    "before it finished, so nothing in it ran. Do not repeat it. Keep each response short: state "
    "your current hypothesis or next step in one or two lines, then make the tool calls, and put "
    "long material such as scripts into files through tool calls instead of the reply."
)
EMPTY_REPLY_NOTICE = (
    "[Harnessix host notice] Your last response was empty: it had no text and no tool call, so "
    "nothing ran. If the task is finished, reply now with a short final summary of what you did "
    "and the result. Otherwise continue with the next tool call."
)
_NOTICES: dict[RecoverableReply, str] = {
    "truncated": TRUNCATED_REPLY_NOTICE,
    "empty": EMPTY_REPLY_NOTICE,
}
_NOT_COMPLETED = "模型未正常完成"
_NO_CONTENT = ("invalid_provider_output", "模型响应没有语义内容")
_UNRECOVERED: dict[RecoverableReply, tuple[str, str]] = {
    "truncated": ("provider_max_output_tokens", _NOT_COMPLETED),
    "empty": _NO_CONTENT,
}


def recovery_notice_item_id(turn_id: UUID, step: int) -> UUID:
    """第step个模型步骤补发提示的稳定身份；一步至多补发一次，两种情况共用。"""
    return uuid5(turn_id, f"{TRUNCATION_RECOVERY_VERSION}:{step}")


def recovery_notice(turn: Turn, step: int, reply: RecoverableReply) -> str | None:
    """第step步的回复不可用时应补发的提示；None表示不再恢复，按原错误失败。"""
    item_ids = {item.item_id for item in turn.items}
    consecutive = 0
    while (
        consecutive < step - 1
        and recovery_notice_item_id(turn.turn_id, step - 1 - consecutive) in item_ids
    ):
        consecutive += 1
    if (
        consecutive >= MAX_CONSECUTIVE_RECOVERY_NOTICES
        or turn.model_steps >= turn.budget.max_steps
        or turn.usage.total_tokens >= turn.budget.max_tokens
    ):
        return None
    return _NOTICES[reply]


def recovery_notice_events(turn: Turn, step: int, reply: RecoverableReply) -> list[EventDraft]:
    """第step步补发提示的事件；补发次数或预算用尽时按该情况原有的错误失败。"""
    notice = recovery_notice(turn, step, reply)
    if notice is None:
        raise KernelError(*_UNRECOVERED[reply])
    return host_notice_events(turn.turn_id, recovery_notice_item_id(turn.turn_id, step), notice)


def recovery_switch(value: object) -> bool:
    """运行时开关只接受字面布尔值；默认关闭时不可用的回复仍按原错误失败。"""
    if type(value) is not bool:
        raise KernelError("invalid_runtime_configuration", "截断恢复开关必须是布尔值")
    return value


def recoverable_reply(
    finish_reason: str, call_ids: object, has_text: bool, enabled: bool
) -> RecoverableReply | None:
    """没有任何工具调用的步骤里，可以补发提示继续的回复属于哪种情况（仅在开启时）。

    以输出上限结束的是截断，有没有正文都算；正常结束却没有正文的是空回复。
    截断的工具调用仍按ADR 0109第2节处理。
    """
    if not enabled or call_ids:
        return None
    if finish_reason == "max_output_tokens":
        return "truncated"
    return "empty" if finish_reason == "completed" and not has_text else None


def settle_reply(
    finish_reason: str, call_ids: object, has_text: bool, recovery_enabled: bool
) -> RecoverableReply | None:
    """判定一个模型步骤的终态：回复可用时返回None，可补发提示继续时返回其情况，否则失败。"""
    recovery = recoverable_reply(finish_reason, call_ids, has_text, recovery_enabled)
    if finish_reason not in {"completed", "tool_calls"} and not recovery:
        raise KernelError("provider_" + finish_reason, _NOT_COMPLETED)
    if bool(call_ids) != (finish_reason == "tool_calls"):
        raise KernelError("invalid_provider_output", "停止原因与 Tool Call 不一致")
    if not call_ids and not has_text and not recovery:
        raise KernelError(*_NO_CONTENT)
    return recovery
