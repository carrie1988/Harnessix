"""Action Artifact发布时的Session快照：调用方观察之后只允许同一Turn的Steering插入。

职责：在发布事务内取当前Thread，并核对调用方快照之后追加的事件全部是该Turn的用户补充输入。
非职责：调用归属、审批与配额校验（由各发布函数在返回的当前Thread上完成）。
"""

from __future__ import annotations

from uuid import UUID

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, ItemFinished, ItemStarted, TextContent, Thread
from harnessix.session.sqlite import SQLiteSessionStore


def _is_steering(event: AgentEvent, turn_id: UUID) -> bool:
    payload = event.payload
    return (
        event.turn_id == turn_id
        and isinstance(payload, ItemStarted | ItemFinished)
        and isinstance(payload.content, TextContent)
        and payload.content.kind == "user_message"
    )


async def action_publication_snapshot(
    session: SQLiteSessionStore,
    database: aiosqlite.Connection,
    thread_id: UUID,
    turn_id: UUID,
    expected_sequence: int,
    *,
    subject: str,
) -> Thread:
    """返回事务内的当前Thread；调用方快照之后出现Steering以外的事件时按sequence_conflict拒绝。

    Steering可以在工具规划与执行期间提交，当前Tool Result先完成，补充输入进入下一模型步骤
    （ADR 0072第8条）；因此它不使正在发布的审批材料或命令输出作废。
    """
    conflict = KernelError("sequence_conflict", f"{subject}发布时会话已变化")
    thread = await session._snapshot(database, thread_id)
    if thread is None or thread.sequence < expected_sequence:
        raise conflict
    if thread.sequence > expected_sequence:
        appended = await session._events(database, thread_id, expected_sequence)
        if not all(_is_steering(event, turn_id) for event in appended):
            raise conflict
    return thread
