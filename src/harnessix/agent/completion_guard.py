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

交互模式（interactive，产品终端界面的默认值，ADR 0147）只保留“宣布还要继续”这一条：用户就在屏幕前，
问答类的回复本来就没有工具调用，不能按“还没有补丁”催促；但模型说了“我先……然后写文件”就停下时，
用户只能再输入一次“继续”。这一模式下首句破折号之后的分句同样算宣布，向用户提问的回复不算。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Literal
from uuid import UUID, uuid5

from harnessix.agent.errors import KernelError
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
CompletionGuardMode = Literal["off", "interactive", "full"]
# 首句里把前后两个分句隔开的破折号（中文破折号，或两侧带空格的短横与长横）。
_CLAUSE_DASH = re.compile(r"——|\s[—–-]\s")
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
    full = mode == "full"
    if full and not_a_reply(reply):
        return NOT_A_REPLY_NOTICE
    if completion_notice_item_id(turn.turn_id, step - 1) in item_ids:
        return None
    if announces_next_step(reply) if full else leaves_a_step_undone(reply):
        return UNFINISHED_STEP_NOTICE
    if full and not workspace_patched(turn.items):
        return NO_CHANGE_NOTICE
    return None


def leaves_a_step_undone(text: str) -> bool:
    """交互模式：回复宣布了下一步却没有任何动作。

    比无人值守多认一种写法：首句破折号之后的分句以宣布的说法开头
    （“No existing files — I'll build …”）。以问号结尾的回复是在等用户回答，不算。
    """
    stripped = text.strip()
    if not stripped or stripped.endswith(("?", "？")):
        return False
    later_clauses = _CLAUSE_DASH.split(stripped.splitlines()[0])[1:]
    return announces_next_step(stripped) or any(
        clause.strip().lower().startswith(_CONTINUATION_OPENINGS) for clause in later_clauses
    )


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
