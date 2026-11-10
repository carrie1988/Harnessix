"""纯文本截断恢复：回复因输出上限中断、又没有工具调用时，宿主补发提示继续，而不是让Turn失败。

ADR 0109只恢复了截断的工具调用（按目录拒绝重入），纯文本截断仍以`provider_max_output_tokens`
结束整个Turn。评测中模型偶尔在一步里写上万字的推导，用满输出上限后整题作废，而它截断前往往
正在自查补丁。显式开启后（产品默认开启），这类步骤改为：

- 截断的正文照常作为助手消息落账（用量照常记账），本步不执行任何东西；
- 宿主以用户消息补发提示，说明回复被截断、要求简短地继续，按Steering语义重入下一步；
- 连续截断最多补发两次；第三次连续截断、或步骤与Token预算已用尽时，仍按输出上限失败。

被剩余预算压低的输出上限造成的截断仍归为预算耗尽（budget_capped_truncation），不在此恢复。
判定只读本Turn已持久化的补发Item，补发Item身份由Turn与步骤派生，重放确定；不新增事件类型。
"""

from __future__ import annotations

from uuid import UUID, uuid5

from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, Turn

TRUNCATION_RECOVERY_VERSION = "harnessix.truncation-recovery/v1"
MAX_CONSECUTIVE_TRUNCATION_NOTICES = 2

TRUNCATED_REPLY_NOTICE = (
    "[Harnessix host notice] Your last response reached the output token limit and was cut off "
    "before it finished, so nothing in it ran. Do not repeat it. Keep each response short: state "
    "your current hypothesis or next step in one or two lines, then make the tool calls, and put "
    "long material such as scripts into files through tool calls instead of the reply."
)


def truncation_notice_item_id(turn_id: UUID, step: int) -> UUID:
    """第step个模型步骤纯文本截断后补发提示的稳定身份。"""
    return uuid5(turn_id, f"{TRUNCATION_RECOVERY_VERSION}:{step}")


def truncation_notice(turn: Turn, step: int) -> str | None:
    """第step步纯文本截断后应补发的提示；None表示不再恢复，按输出上限失败。"""
    item_ids = {item.item_id for item in turn.items}
    consecutive = 0
    while (
        consecutive < step - 1
        and truncation_notice_item_id(turn.turn_id, step - 1 - consecutive) in item_ids
    ):
        consecutive += 1
    if (
        consecutive >= MAX_CONSECUTIVE_TRUNCATION_NOTICES
        or turn.model_steps >= turn.budget.max_steps
        or turn.usage.total_tokens >= turn.budget.max_tokens
    ):
        return None
    return TRUNCATED_REPLY_NOTICE


def truncation_notice_events(turn: Turn, step: int) -> list[EventDraft]:
    """第step步纯文本截断后的补发提示事件；补发次数或预算用尽时按输出上限失败。"""
    notice = truncation_notice(turn, step)
    if notice is None:
        raise KernelError("provider_max_output_tokens", "模型未正常完成")
    return host_notice_events(turn.turn_id, truncation_notice_item_id(turn.turn_id, step), notice)


def recovery_switch(value: object) -> bool:
    """运行时开关只接受字面布尔值；默认关闭时纯文本截断仍按输出上限失败。"""
    if type(value) is not bool:
        raise KernelError("invalid_runtime_configuration", "截断恢复开关必须是布尔值")
    return value


def recoverable_truncation(
    finish_reason: str, call_ids: object, has_text: bool, enabled: bool
) -> bool:
    """以输出上限结束、没有任何工具调用且有正文的步骤可以补发提示继续（仅在开启时）。

    截断的工具调用仍按ADR 0109第2节处理；没有正文的截断没有可保留的内容，照旧失败。
    """
    return enabled and finish_reason == "max_output_tokens" and not call_ids and has_text
