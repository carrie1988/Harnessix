"""请求只追加：预算与可执行工具的宿主说明落为历史Item，不再逐请求附在末尾（ADR 0132）。

逐请求附加的说明每次都不同，下一请求又要把旧的去掉：服务端前缀缓存只能复用到旧说明之前，
模型刚生成的回复也得重算。显式开启后（AgentRuntime的history_notes）：

- 预算说明只在跨过关口时各发一次：剩一半、剩四分之一、最后十次调用；每个关口都同时按步数与
  累计Token判断，“最后十次”的Token等价是“按上一请求的尺寸只够再发十次”；
- 目录说明只在配置了工具视图时发：每个Turn的首个请求一条，此后本步可执行集合或选择约束与
  本Turn上一条目录说明不同时再发一条；没有工具视图时可执行集合整轮不变，不发；
- 说明是已完成的用户消息Item，之后一直留在历史里，请求因而等于“上一请求＋新内容”。

判定只读本Turn已持久化的事实；说明Item的身份由Turn与关口（或目录说明序号）派生，重放确定。
非职责：不做预算准入（仍由Runtime与Reducer负责），不改变可执行集合。
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from enum import IntEnum
from typing import Protocol
from uuid import UUID, uuid5

from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.models import EventDraft, TextContent, Turn, TurnStatus
from harnessix.agent.ports import ModelToolSelection, ModelToolView
from harnessix.agent.reducer import get_turn
from harnessix.models._history import tool_catalog_update
from harnessix.session.ports import SessionStore

REQUEST_NOTES_VERSION = "harnessix.request-notes/v1"
# “最后十次调用”关口：留给收尾（落实改动、验证、可能的一次修复、最终答复）的调用数。
FINAL_CALLS = 10
_HOST_NOTICE = "[Harnessix host notice] "


class BudgetLevel(IntEnum):
    """预算关口，数值越大越紧；一个Turn内每个关口至多提醒一次，且只向更紧的关口推进。"""

    HALF = 1
    QUARTER = 2
    FINAL = 3


_ADVICE = {
    BudgetLevel.HALF: (
        "About half of the budget is used. Decide what is still needed to finish and verify "
        "the work, and leave out the rest."
    ),
    BudgetLevel.QUARTER: (
        "About a quarter of the budget is left. Finish the work now and verify it."
    ),
    BudgetLevel.FINAL: (
        "The task stops when the budget runs out. Stop exploring: make sure your work is in "
        "place, check it once with the most relevant test or command, then reply with your "
        "final summary."
    ),
}


class RequestNotesHost(Protocol):
    store: SessionStore
    _history_notes: bool
    _model_tool_view: ModelToolView | None

    def _model_tools_for_turn(self, turn: Turn) -> ModelToolSelection: ...

    def _lock(self, thread_id: UUID) -> AbstractAsyncContextManager[None]: ...


def budget_note_item_id(turn_id: UUID, level: BudgetLevel) -> UUID:
    """本Turn某个预算关口说明的稳定身份。"""
    return uuid5(turn_id, f"{REQUEST_NOTES_VERSION}:budget:{level.name.lower()}")


def catalog_note_item_id(turn_id: UUID, ordinal: int) -> UUID:
    """本Turn第ordinal条（从0起）目录说明的稳定身份。"""
    return uuid5(turn_id, f"{REQUEST_NOTES_VERSION}:catalog:{ordinal}")


def request_note_ids(turn_id: UUID, item_ids: Iterable[UUID]) -> frozenset[UUID]:
    """item_ids（本Turn的Item，按持久顺序）中属于预算或目录说明的身份。

    目录说明按序号递增识别，所以要求顺序完整；界面与工具视图据此把说明与真实用户输入分开。
    """
    budget = {budget_note_item_id(turn_id, level) for level in BudgetLevel}
    ordinal = 0
    next_catalog = catalog_note_item_id(turn_id, ordinal)
    found: set[UUID] = set()
    for item_id in item_ids:
        if item_id in budget:
            found.add(item_id)
        elif item_id == next_catalog:
            found.add(item_id)
            ordinal += 1
            next_catalog = catalog_note_item_id(turn_id, ordinal)
    return frozenset(found)


def without_request_notes(turn: Turn) -> Turn:
    """给工具视图的独立副本：说明不是用户的新输入，不应改变视图对“最近一次用户输入”的判断。"""
    notes = request_note_ids(turn.turn_id, (item.item_id for item in turn.items))
    kept = tuple(item for item in turn.items if item.item_id not in notes)
    return turn.model_copy(update={"items": kept}).model_copy(deep=True)


def calls_left(turn: Turn) -> int:
    """还能发起的主模型请求数（含即将发起的这一次）；失败重试与主步骤共用步数预算。"""
    attempts = turn.model_attempts
    retries = len(attempts) - len({attempt.step for attempt in attempts})
    return max(0, turn.budget.max_steps - turn.model_steps - retries)


def affordable_calls(turn: Turn) -> int | None:
    """剩余Token按最近一次完整请求的实际输入＋输出折算还能发几次；没有完整用量时不折算。"""
    last = next((a.usage for a in reversed(turn.model_attempts) if a.status == "completed"), None)
    if last is None or not last.input_tokens:
        return None
    size = last.input_tokens + (last.output_tokens or 0)
    return _tokens_left(turn) // size


def budget_level(turn: Turn) -> BudgetLevel | None:
    """当前所处的最紧关口；预算用掉不到一半时为None。

    “最后十次”只在剩余不足四分之一时成立：步数预算本来就小的任务，不在刚开始时就催促收尾。
    """
    calls, tokens, budget = calls_left(turn), _tokens_left(turn), turn.budget

    def within(divisor: int) -> bool:
        return calls * divisor <= budget.max_steps or tokens * divisor <= budget.max_tokens

    if not within(2):
        return None
    if not within(4):
        return BudgetLevel.HALF
    affordable = affordable_calls(turn)
    if calls <= FINAL_CALLS or (affordable is not None and affordable <= FINAL_CALLS):
        return BudgetLevel.FINAL
    return BudgetLevel.QUARTER


def budget_note(turn: Turn, level: BudgetLevel) -> str:
    """关口说明正文：先给宿主账本里的事实，再给一句下一步建议。

    Token只在用掉一半以上时才写出：交互使用不设Token上限，写出来只是干扰。
    """
    budget, tokens = turn.budget, _tokens_left(turn)
    facts = f"{calls_left(turn)} of {budget.max_steps} model calls are left"
    if tokens * 2 <= budget.max_tokens:
        facts += f"; {tokens:,} of {budget.max_tokens:,} tokens are left"
        affordable = affordable_calls(turn)
        if affordable is not None:
            facts += f", enough for about {affordable} more calls at the current request size"
    return f"{_HOST_NOTICE}Budget checkpoint: {facts}. {_ADVICE[level]}"


def request_note_events(turn: Turn, catalog: str | None) -> list[EventDraft]:
    """下一步模型请求之前到期的说明事件；没有到期的说明时为空。

    catalog是本步目录说明的完整正文（没有工具视图时为None）：与本Turn上一条不同才发。
    更紧的预算关口已提醒过时，不再补发较松的关口。
    """
    turn_id = turn.turn_id
    present = {item.item_id for item in turn.items}
    events: list[EventDraft] = []
    level = budget_level(turn)
    if level is not None and not any(
        budget_note_item_id(turn_id, seen) in present for seen in BudgetLevel if seen >= level
    ):
        events += host_notice_events(
            turn_id, budget_note_item_id(turn_id, level), budget_note(turn, level)
        )
    if catalog is not None:
        sent = _catalog_notes(turn)
        if not sent or sent[-1] != catalog:
            events += host_notice_events(turn_id, catalog_note_item_id(turn_id, len(sent)), catalog)
    return events


async def append_request_notes(host: RequestNotesHost, thread_id: UUID, turn: Turn) -> None:
    """为下一步模型请求补记到期的说明；宿主未开启history_notes时不做任何事。

    先用调用方手里的Turn快照判断，绝大多数步骤没有到期的说明，不加锁也不读Session；
    有到期的说明时在线程CAS内重读并重判后追加。Turn已离开准备阶段（如正在取消）时不写。
    """
    if not host._history_notes or not request_note_events(turn, _catalog_note(host, turn)):
        return
    async with host._lock(thread_id):
        thread = await host.store.get_thread(thread_id)
        current = get_turn(thread, turn.turn_id)
        if current.status != TurnStatus.PREPARING_CONTEXT:
            return
        events = request_note_events(current, _catalog_note(host, current))
        if events:
            await host.store.append(thread_id, events, expected_sequence=thread.sequence)


def _tokens_left(turn: Turn) -> int:
    return max(0, turn.budget.max_tokens - turn.usage.total_tokens)


def _catalog_notes(turn: Turn) -> list[str]:
    """本Turn已发出的目录说明正文，按发出顺序。"""
    texts: list[str] = []
    expected = catalog_note_item_id(turn.turn_id, 0)
    for item in turn.items:
        if item.item_id == expected:
            assert isinstance(item.content, TextContent)
            texts.append(item.content.text)
            expected = catalog_note_item_id(turn.turn_id, len(texts))
    return texts


def _catalog_note(host: RequestNotesHost, turn: Turn) -> str | None:
    """本步的目录说明正文；没有工具视图时可执行集合与选择约束整轮不变，不需要说明。"""
    if host._model_tool_view is None:
        return None
    selection = host._model_tools_for_turn(turn)
    return _HOST_NOTICE + tool_catalog_update(
        (tool.name for tool in selection.tools),
        tool_choice=selection.choice,
        forced_tool=selection.forced_tool,
        notice=selection.notice,
    )
