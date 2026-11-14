"""完成守卫：无人值守时，不像完成的纯文本回复不终结Turn，改由宿主补发继续提示。

模型一步没有工具调用即被视为最终答复（AgentRuntime._close_model_step）。评测中小模型偶尔只写一句
进展、照抄上下文摘要、输出一段截断的JSON或把工具调用写进正文就停下，整题以空补丁结束。显式开启后
（产品配置completion_guard），这类回复改为由宿主以用户消息补发提示、按Steering重入下一步：

- 回复形态不是答复（reply_shape：空、宿主记录、截断或写坏的结构化片段、写成文本的工具调用）：
  按形态补发对应提示；只包着文本的线上外壳先去壳再判定；
- 上一步是对补发的回应：照常完成。以下两条靠措辞与状态推断，问一次即止，不循环；
- 回复宣布还要继续（开头或结尾句说了自己的下一步，或以冒号结尾）：说明漏了工具调用，补发提示；
- 本Turn尚无成功的补丁（apply_patch_batch/apply_patch）：补发提示；用其他方式改过文件或确实无需
  修改的任务只多花一步。

补发次数随可观察状态变化：还看得出没做完（尚无成功补丁、计划有未完成步骤、最近一次工具结果失败）
时放宽到硬上限，否则为基础值；步骤或Token预算已用尽时不补发。每条提示写明剩余模型调用次数。

交互模式（interactive，产品终端界面的默认值，ADR 0147）只保留“宣布还要继续”这一条：用户就在屏幕前，
问答类的回复本来就没有工具调用，不能按“还没有补丁”催促；但模型说了“我先……然后写文件”就停下时，
用户只能再输入一次“继续”。这一模式下首句破折号之后的分句同样算宣布，向用户提问的回复不算；
补发次数取基础值（没有补丁不算未完成迹象），提示不附剩余调用次数。

判定只读本Turn已持久化的事实与本步回复正文，重放确定；补发Item的身份由Turn与步骤派生。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal
from uuid import UUID, uuid5

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    EventDraft,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PlanContent,
    TextContent,
    ToolResultContent,
    Turn,
)
from harnessix.agent.reply_shape import (
    HOST_RECORD_MARKERS,
    ReplyShape,
    announces_next_step,
    leaves_a_step_undone,
    reply_shape,
    reply_text,
)
from harnessix.agent.trusted_action_contracts import TrustedActionApprovalRequestContent

COMPLETION_GUARD_VERSION = "harnessix.completion-guard/v1"
CompletionGuardMode = Literal["off", "interactive", "full"]
# 看不出未完成迹象时每Turn的补发次数。
BASE_COMPLETION_NOTICES = 2
# 硬上限：任何状态下每Turn的补发都不超过此数，守卫因此必然终止，与步骤预算无关。
MAX_COMPLETION_NOTICES = 6

NOT_A_REPLY_NOTICE = (
    "[Harnessix host notice] Your last response had no tool call, so it would end the task, "
    "but it was not a final answer: it was empty or a JSON record (context summaries and tool "
    "errors are host records, not replies). The task is not finished. Continue with tool "
    "calls; when the work is done and verified, reply with a short plain-text summary."
)
CUT_OFF_REPLY_NOTICE = (
    "[Harnessix host notice] Your last response had no tool call, so it would end the task, "
    "but its text was an unfinished tool call or JSON record: it was cut off or malformed, so "
    "nothing ran. The task is not finished. Send the call again through the tool interface in "
    "a smaller form: one short call at a time, splitting a large edit or command into several "
    "calls."
)
CALL_AS_TEXT_NOTICE = (
    "[Harnessix host notice] Your last response wrote a tool call as text instead of calling "
    "the tool, so nothing ran and it would end the task. The host runs only calls made through "
    "the tool interface, never calls found in text. Make that call now as a real tool call; if "
    "the task is already complete and verified, reply with a short plain-text summary without "
    "call markup."
)
NO_CHANGE_NOTICE = (
    "[Harnessix host notice] Your last response had no tool call, so it would end the task, "
    "but no apply_patch_batch change has been made in this task yet. If the task needs a "
    "code change, continue with tool calls. If it is already complete, reply with your final "
    "summary again."
)
UNFINISHED_STEP_NOTICE = (
    "[Harnessix host notice] Your last response announced a next step but had no tool call, so "
    "nothing ran and it would end the task. Make the tool calls for that step now; if the task "
    "is already complete and verified, reply with a short plain-text summary instead."
)
_NON_ANSWER_NOTICES: dict[ReplyShape, str] = {
    "empty": NOT_A_REPLY_NOTICE,
    "host_record": NOT_A_REPLY_NOTICE,
    "cut_off": CUT_OFF_REPLY_NOTICE,
    "call_as_text": CALL_AS_TEXT_NOTICE,
}
# 模型历史里替换结构化乱码的占位；开头属于HOST_RECORD_MARKERS，模型照抄它同样不算答复。
NON_ANSWER_PLACEHOLDER = (
    "[Harnessix host placeholder] The host removed a response here: it was a JSON record or "
    "an unfinished tool call written as text, not an answer."
)


def completion_notice_item_id(turn_id: UUID, step: int) -> UUID:
    """第step个模型步骤之后补发提示的稳定身份。"""
    return uuid5(turn_id, f"{COMPLETION_GUARD_VERSION}:{step}")


def completion_guard_mode(value: object) -> CompletionGuardMode:
    """运行时开关：布尔值（True为完整守卫）或模式名；其他取值按配置错误拒绝。"""
    if value is True or value == "full":
        return "full"
    if value is False or value == "off":
        return "off"
    if value == "interactive" and type(value) is str:
        return "interactive"
    raise KernelError("invalid_runtime_configuration", "完成守卫开关必须是布尔值或已定义的模式")


def completion_notice(
    turn: Turn, step: int, reply: str, mode: CompletionGuardMode = "full"
) -> str | None:
    """第step步无工具调用、正文为reply时应补发的提示；None表示照常完成。"""
    full = mode == "full"
    remaining_calls = turn.budget.max_steps - turn.model_steps
    # 交互模式的问答本来就没有补丁，不按未完成迹象放宽次数。
    allowance = notice_allowance(turn.items) if full else BASE_COMPLETION_NOTICES
    if (
        remaining_calls <= 0
        or turn.usage.total_tokens >= turn.budget.max_tokens
        or _notices_sent(turn, step) >= allowance
    ):
        return None
    reason = _continue_reason(turn, step, reply_text(reply), full=full)
    if reason is None or not full:
        return reason
    # 剩余步数是宿主已知的事实；写进提示，不依赖Provider是否支持逐请求的预算说明。
    return f"{reason} Model calls left in this task: at most {remaining_calls}."


def _continue_reason(turn: Turn, step: int, text: str, *, full: bool) -> str | None:
    """去壳后的正文不足以结束Turn的原因；None表示它是可以接受的答复。

    交互模式（full为False）只看“宣布还要继续”：不是答复的形态由回复恢复负责，没有补丁不算未完成。
    """
    if full:
        shape = reply_shape(text)
        if shape != "answer":
            return _NON_ANSWER_NOTICES[shape]
    previous_notice = completion_notice_item_id(turn.turn_id, step - 1)
    if any(item.item_id == previous_notice for item in turn.items):
        return None
    if announces_next_step(text) if full else leaves_a_step_undone(text):
        return UNFINISHED_STEP_NOTICE
    if full and not workspace_patched(turn.items):
        return NO_CHANGE_NOTICE
    return None


def _notices_sent(turn: Turn, step: int) -> int:
    item_ids = {item.item_id for item in turn.items}
    return sum(
        completion_notice_item_id(turn.turn_id, earlier) in item_ids for earlier in range(1, step)
    )


def notice_allowance(items: Sequence[Item]) -> int:
    """本Turn可补发的次数：还看得出没做完时放宽到硬上限，否则为基础值。

    提前结束不可逆（整题作废），多问一次只花一步；因此有未完成迹象时宁可多问，但次数始终有界。
    """
    unfinished = (
        not workspace_patched(items) or plan_unfinished(items) or last_tool_call_failed(items)
    )
    return MAX_COMPLETION_NOTICES if unfinished else BASE_COMPLETION_NOTICES


def completion_notice_events(turn_id: UUID, step: int, notice: str | None) -> list[EventDraft]:
    """补发提示的用户消息Item事件；notice为None时为空。"""
    if notice is None:
        return []
    return host_notice_events(turn_id, completion_notice_item_id(turn_id, step), notice)


def host_notice_events(turn_id: UUID, item_id: UUID, notice: str) -> list[EventDraft]:
    """宿主补发提示落为一条已完成的用户消息Item；身份由调用方按Turn与步骤派生，重放确定。"""
    content = TextContent(kind="user_message", text=notice)
    return [
        EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=item_id, content=content)),
        EventDraft(
            turn_id=turn_id,
            payload=ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
        ),
    ]


def step_reply(items: Iterable[Item], history_ids: set[UUID]) -> str:
    """本步模型输出的助手正文：位于本次模型历史最后一项之后的assistant_message。"""
    ordered = tuple(items)
    last_history = max(
        (index for index, item in enumerate(ordered) if item.item_id in history_ids), default=-1
    )
    return "".join(
        item.content.text
        for item in ordered[last_history + 1 :]
        if isinstance(item.content, TextContent) and item.content.kind == "assistant_message"
    )


def workspace_patched(items: Iterable[Item]) -> bool:
    """本Turn是否有成功的补丁：Trusted Action补丁组（产品路径）或旧补丁服务的效果。"""
    contents = [item.content for item in items]
    patch_calls = {
        content.call_id
        for content in contents
        if isinstance(content, TrustedActionApprovalRequestContent)
        and content.presentation == "patch_batch"
    }
    return any(
        isinstance(content, ToolResultContent)
        and content.outcome == "succeeded"
        and (
            content.call_id in patch_calls
            or content.patch is not None
            or content.patch_batch is not None
        )
        for content in contents
    )


def plan_unfinished(items: Iterable[Item]) -> bool:
    """本Turn最新的计划是否还有未完成的步骤；本Turn没有更新过计划时为False。"""
    plans = [item.content for item in items if isinstance(item.content, PlanContent)]
    return bool(plans) and any(step.status != "completed" for step in plans[-1].steps)


def last_tool_call_failed(items: Iterable[Item]) -> bool:
    """本Turn最近一次工具结果是否不是成功；还没有工具结果时为False。"""
    results = [item.content for item in items if isinstance(item.content, ToolResultContent)]
    return bool(results) and results[-1].outcome != "succeeded"


def without_non_answers(history: Sequence[Item]) -> tuple[Item, ...]:
    """发给模型的历史：守卫已判为不是答复的结构化乱码换成短占位，持久Item不变。

    乱码留在历史里会被模型当成自己的上一条回复再抄一遍。只替换紧跟“不是答复”类提示、形态仍是
    结构化片段且比占位长的助手消息：提示证明守卫在此判定过，形态与长度保证不丢正文、不增加输入。
    照抄宿主记录的回复不替换，它与宿主自己投影的上下文摘要无法区分。
    """
    visible = list(history)
    for index, item in enumerate(visible[:-1]):
        # 先看后一条是不是提示：每次出站映射都会走到这里，形态判定只留给极少数候选。
        if _is_non_answer_notice(visible[index + 1]) and _is_structured_non_answer(item):
            placeholder = TextContent(kind="assistant_message", text=NON_ANSWER_PLACEHOLDER)
            visible[index] = item.model_copy(update={"content": placeholder})
    return tuple(visible)


def _is_non_answer_notice(item: Item) -> bool:
    return (
        isinstance(item.content, TextContent)
        and item.content.kind == "user_message"
        and item.content.text.startswith(tuple(_NON_ANSWER_NOTICES.values()))
    )


def _is_structured_non_answer(item: Item) -> bool:
    content = item.content
    if not isinstance(content, TextContent) or content.kind != "assistant_message":
        return False
    return (
        len(content.text) > len(NON_ANSWER_PLACEHOLDER)
        and not any(marker in content.text for marker in HOST_RECORD_MARKERS)
        and reply_shape(reply_text(content.text)) in ("host_record", "cut_off", "call_as_text")
    )
