"""完成守卫：无人值守时，不像完成的纯文本回复不终结Turn，改由宿主补发继续提示。

模型一步没有工具调用即被视为最终答复（AgentRuntime._close_model_step）。评测中小模型偶尔只写一句
进展、照抄上下文摘要JSON或编造错误JSON就停下，整题以空补丁结束。显式开启后（产品配置
completion_guard），这类回复改为由宿主以用户消息补发提示、按Steering重入下一步：

- 回复为空，或是一个JSON对象、含上下文摘要标记：不是答复，补发提示；
- 本Turn尚无成功的补丁（apply_patch_batch/apply_patch），且上一步不是对补发的回应：补发提示；
  补发后模型再次给出纯文本结论即照常完成，用其他方式改过文件的模型只多花一步；
- 回复宣布还要继续（以“下一步”“让我”“我将”等开头，或以冒号结尾），且上一步不是对补发的回应：
  说明漏了工具调用，补发提示；这与是否已有补丁无关；
- 每个Turn最多补发两次；步骤或Token预算已用尽时不补发，照常完成。

判定只读本Turn已持久化的事实与本步回复正文，重放确定；补发Item的身份由Turn与步骤派生。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from uuid import UUID, uuid5

from harnessix.agent.models import (
    EventDraft,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ToolResultContent,
    Turn,
)
from harnessix.agent.trusted_action_contracts import TrustedActionApprovalRequestContent

COMPLETION_GUARD_VERSION = "harnessix.completion-guard/v1"
MAX_COMPLETION_NOTICES = 2
_SUMMARY_SCHEMA_MARKER = "harnessix.compaction-summary"
_SUMMARY_HEADER_MARKER = "[Host summary of earlier history"

NOT_A_REPLY_NOTICE = (
    "[Harnessix host notice] Your last response had no tool call, so it would end the task, "
    "but it was not a final answer: it was empty or a JSON record (context summaries and tool "
    "errors are host records, not replies). The task is not finished. Continue with tool "
    "calls; when the work is done and verified, reply with a short plain-text summary."
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
# 宣布下一步的开头（英文与中文，小写比较）；回复以冒号结尾同样表示后面应跟着动作。
_CONTINUATION_OPENINGS = (
    "next step",
    "next,",
    "next i",
    "let me",
    "let's",
    "i'll",
    "i will",
    "i need to",
    "now i",
    "now let",
    "下一步",
    "接下来",
    "让我",
    "我将",
    "我先",
    "我需要",
    "现在我",
)


def completion_notice_item_id(turn_id: UUID, step: int) -> UUID:
    """第step个模型步骤之后补发提示的稳定身份。"""
    return uuid5(turn_id, f"{COMPLETION_GUARD_VERSION}:{step}")


def completion_notice(turn: Turn, step: int, reply: str) -> str | None:
    """第step步无工具调用、正文为reply时应补发的提示；None表示照常完成。"""
    item_ids = {item.item_id for item in turn.items}
    sent = sum(
        completion_notice_item_id(turn.turn_id, earlier) in item_ids for earlier in range(1, step)
    )
    if (
        sent >= MAX_COMPLETION_NOTICES
        or turn.model_steps >= turn.budget.max_steps
        or turn.usage.total_tokens >= turn.budget.max_tokens
    ):
        return None
    if not_a_reply(reply):
        return NOT_A_REPLY_NOTICE
    if completion_notice_item_id(turn.turn_id, step - 1) in item_ids:
        return None
    if announces_next_step(reply):
        return UNFINISHED_STEP_NOTICE
    if not workspace_patched(turn.items):
        return NO_CHANGE_NOTICE
    return None


def announces_next_step(text: str) -> bool:
    """回复以宣布下一步的说法开头或以冒号结尾：后面本应跟着工具调用。"""
    # 去掉行首Markdown标记（如“**Next step:**”）后再比较开头。
    stripped = text.strip().lstrip("*#>-_` ").lower()
    return stripped.startswith(_CONTINUATION_OPENINGS) or stripped.endswith((":", "："))


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


def not_a_reply(text: str) -> bool:
    """空正文、JSON对象或照抄的上下文摘要（v1 JSON或v2说明头）都不是给用户的答复。"""
    stripped = text.strip()
    if not stripped or _SUMMARY_SCHEMA_MARKER in stripped or _SUMMARY_HEADER_MARKER in stripped:
        return True
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except ValueError:
        return False


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
