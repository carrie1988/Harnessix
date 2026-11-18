"""不可用回复的恢复：回复没有工具调用、又不能当作答复时，宿主补发提示继续，而不是让Turn失败。

三种情况（ADR 0109第3、6、7版），显式开启后生效（产品默认开启）：

- 截断：回复在输出上限处中断。评测中模型偶尔在一步里写上万字的推导，用满输出上限后整题作废；
  思考模型也可能把输出额度全部用在推理上、正文一个字没有。
- 空回复：回复正常结束，却没有正文也没有工具调用。思考模型偶尔只推理、不作答就结束，
  往往发生在任务已经做完、只差一句总结的时候。
- 工具调用写成了正文：回复正常结束、没有工具调用，正文却是一次工具调用的文字形式
  （`<tool_call>`、`<function=…>`或带本步工具名的`{"name": …, "arguments": …}`）。此前它被当作
  最终答复，界面显示“已完成”，其实什么也没执行（Beta Issue #40）。

处理相同：

- 已有的正文照常作为助手消息落账（用量照常记账），本步不执行任何东西；
- 宿主以用户消息补发提示，说明发生了什么、要求继续，按Steering语义重入下一步；
- 连续最多补发两次（各情况合并计数）；第三次，或步骤与Token预算已用尽时，仍按原错误失败
  （工具调用写成正文时为`invalid_provider_output`，不再记为完成）。

被剩余预算压低的输出上限造成的截断仍归为预算耗尽（budget_capped_truncation），不在此恢复。
判定只读本Turn已持久化的补发Item，补发Item身份由Turn与步骤派生，重放确定；不新增事件类型。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid5

from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, Turn
from harnessix.models.contracts import ModelRequest

# 身份串沿用第3版的名字：已持久化的补发Item由它派生，改名会使重放得到不同的Item。
TRUNCATION_RECOVERY_VERSION = "harnessix.truncation-recovery/v1"
MAX_CONSECUTIVE_RECOVERY_NOTICES = 2

RecoverableReply = Literal["truncated", "empty", "tool_call_text"]

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
TOOL_CALL_TEXT_NOTICE = (
    "[Harnessix host notice] Your last response wrote a tool call as plain text instead of "
    "calling the tool, so nothing ran. Do not repeat it as text. Make the call through the "
    "tool-calling interface. If the task is already finished, reply with a short final summary "
    "and no tool call text."
)
_NOTICES: dict[RecoverableReply, str] = {
    "truncated": TRUNCATED_REPLY_NOTICE,
    "empty": EMPTY_REPLY_NOTICE,
    "tool_call_text": TOOL_CALL_TEXT_NOTICE,
}
_NOT_COMPLETED = "模型未正常完成"
_NO_CONTENT = ("invalid_provider_output", "模型响应没有语义内容")
_UNRECOVERED: dict[RecoverableReply, tuple[str, str]] = {
    "truncated": ("provider_max_output_tokens", _NOT_COMPLETED),
    "empty": _NO_CONTENT,
    "tool_call_text": ("invalid_provider_output", "模型把工具调用写成了正文，没有执行"),
}
# 工具调用的文字标记：Hermes/Qwen的<tool_call>（含<previous_tool_call>）、<function=…>与
# </function>、Anthropic的<invoke name=…>、部分模型的<|tool_call…|>。只在同时提到本步工具名时算数。
_TOOL_CALL_MARKUP = re.compile(
    r"<\s*/?\s*(?:previous_)?tool_call\b|<\s*function\s*=|<\s*/\s*function\s*>"
    r"|<\s*invoke\s+name\s*=|<\|tool_call",
    re.IGNORECASE,
)


class _Text(Protocol):
    @property
    def text(self) -> str: ...


class StepTexts(Protocol):
    """一个模型步骤已收到的正文块与请求（model_text.ModelTextPublication）。"""

    @property
    def items(self) -> Mapping[str, _Text]: ...

    @property
    def request(self) -> ModelRequest: ...


@dataclass(frozen=True, slots=True)
class Reply:
    """一个模型步骤的正文与本步可调用的工具名（用于认出写成正文的工具调用）。"""

    text: str
    tool_names: tuple[str, ...] = ()

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    @classmethod
    def of(cls, texts: StepTexts) -> Reply:
        text = "".join(item.text for item in texts.items.values())
        return cls(text, tuple(definition.name for definition in texts.request.tools))


def tool_call_written_as_text(reply: Reply) -> bool:
    """正文是一次工具调用的文字形式：带本步工具名的name/arguments对象，或工具调用标记加工具名。"""
    names = _wire_forms(reply.tool_names)
    if not names or not reply.text:
        return False
    for name in names:
        call = r'"name"\s*:\s*"' + re.escape(name) + r'"\s*,\s*"(?:arguments|parameters)"\s*:'
        if re.search(call, reply.text):
            return True
    return bool(_TOOL_CALL_MARKUP.search(reply.text)) and any(n in reply.text for n in names)


def _wire_forms(names: Iterable[str]) -> set[str]:
    """原名与线上别名（不合法字符换成下划线，如exec_command.shell→exec_command_shell）。"""
    return {form for name in names for form in (name, re.sub(r"[^A-Za-z0-9_-]", "_", name)) if form}


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
    finish_reason: str, call_ids: object, reply: Reply, enabled: bool
) -> RecoverableReply | None:
    """没有任何工具调用的步骤里，可以补发提示继续的回复属于哪种情况（仅在开启时）。

    以输出上限结束的是截断，有没有正文都算；正常结束却没有正文的是空回复；正常结束、正文是
    一次工具调用的文字形式的是写成正文的工具调用。截断的工具调用仍按ADR 0109第2节处理。
    """
    if not enabled or call_ids:
        return None
    if finish_reason == "max_output_tokens":
        return "truncated"
    if finish_reason != "completed":
        return None
    if not reply.has_text:
        return "empty"
    return "tool_call_text" if tool_call_written_as_text(reply) else None


def settle_reply(
    finish_reason: str, call_ids: object, texts: StepTexts, recovery_enabled: bool
) -> RecoverableReply | None:
    """判定一个模型步骤的终态：回复可用时返回None，可补发提示继续时返回其情况，否则失败。"""
    reply = Reply.of(texts)
    has_text = reply.has_text
    recovery = recoverable_reply(finish_reason, call_ids, reply, recovery_enabled)
    if finish_reason not in {"completed", "tool_calls"} and not recovery:
        raise KernelError("provider_" + finish_reason, _NOT_COMPLETED)
    if bool(call_ids) != (finish_reason == "tool_calls"):
        raise KernelError("invalid_provider_output", "停止原因与 Tool Call 不一致")
    if not call_ids and not has_text and not recovery:
        raise KernelError(*_NO_CONTENT)
    return recovery
