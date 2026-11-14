"""工具步骤说明：一步的工具结果全部落账后，把宿主已观察到的事实补发给模型。

模型改坏了原本通过的检查时，常把失败归咎于环境，而“哪条命令在哪次补丁之前通过、之后失败”
宿主早已落账。说明的内容由注入的来源（ToolStepNoticeSource）只读Thread持久事实推导，本模块
只负责发布（ADR 0135）：

- 说明落为紧随该步工具结果的一条用户消息Item，与完成守卫共用host_notice_events，进入持久历史，
  后续请求只在末尾追加。不写进工具结果本身：命令结果的output与输出Artifact逐字段绑定，
  补丁结果与审计摘要绑定，追加字段会使它们校验失败。
- Item身份由Turn与步骤派生，恢复重入不重复发布。
- 当前Turn的用户消息在压缩时固定保留，所以每个Turn至多MAX_STEP_NOTICES_PER_TURN条、
  每条至多MAX_STEP_NOTICE_CHARS字符。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID, uuid5

from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.models import EventPayload, Thread, Turn, TurnStatus
from harnessix.agent.ports import ToolStepNoticeSource
from harnessix.agent.reducer_support import get_turn
from harnessix.session.ports import SessionStore

STEP_NOTICE_VERSION = "harnessix.tool-step-notice/v1"
MAX_STEP_NOTICES_PER_TURN = 6
MAX_STEP_NOTICE_CHARS = 2000
_HOST_NOTICE_PREFIX = "[Harnessix host notice] "
# 工具结果落账后Turn只会停在这两个状态；取消等其他状态不再接受新的用户消息Item。
_OPEN_STATUSES = frozenset({TurnStatus.EXECUTING_TOOLS, TurnStatus.PREPARING_CONTEXT})


class StepNoticeHost(Protocol):
    store: SessionStore
    _step_notices: ToolStepNoticeSource | None

    async def _commit(
        self, thread_id: UUID, turn_id: UUID, payloads: Sequence[EventPayload]
    ) -> Thread: ...


def step_notice_item_id(turn_id: UUID, step: int) -> UUID:
    """第step个模型步骤的工具结果之后补发说明的稳定身份。"""
    return uuid5(turn_id, f"{STEP_NOTICE_VERSION}:{step}")


def step_notice_text(source: ToolStepNoticeSource, thread: Thread, turn: Turn) -> str | None:
    """当前步骤应补发的说明正文；本步已发布、达到每Turn上限或来源没有事实时为None。"""
    item_ids = {item.item_id for item in turn.items}
    sent = sum(
        step_notice_item_id(turn.turn_id, step) in item_ids
        for step in range(1, turn.model_steps + 1)
    )
    if (
        turn.status not in _OPEN_STATUSES
        or step_notice_item_id(turn.turn_id, turn.model_steps) in item_ids
        or sent >= MAX_STEP_NOTICES_PER_TURN
    ):
        return None
    facts = source.step_notice(thread, turn)
    if not facts:
        return None
    return (_HOST_NOTICE_PREFIX + facts)[:MAX_STEP_NOTICE_CHARS]


async def append_step_notice(host: StepNoticeHost, thread_id: UUID, turn_id: UUID) -> None:
    """在一步的全部工具结果落账之后调用；有事实时追加一条宿主说明，否则不产生事件。"""
    source = host._step_notices
    if source is None:
        return
    thread = await host.store.get_thread(thread_id)
    turn = get_turn(thread, turn_id)
    text = step_notice_text(source, thread, turn)
    if text is None:
        return
    drafts = host_notice_events(turn_id, step_notice_item_id(turn_id, turn.model_steps), text)
    await host._commit(thread_id, turn_id, [draft.payload for draft in drafts])
