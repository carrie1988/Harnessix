"""读取并选择当前Workspace的Thread；只读协议查询，不持有Controller状态。"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.projection import ProductViewState
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.protocol.contracts import ThreadView

MAX_PRODUCT_THREADS: Final = 1000


def _ordered_threads(threads: tuple[ThreadView, ...]) -> tuple[ThreadView, ...]:
    return tuple(
        sorted(threads, key=lambda item: (item.updated_at, str(item.thread_id)), reverse=True)
    )


async def _read_all_threads(session: RecoverableAgentSession) -> tuple[ThreadView, ...]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    values: list[ThreadView] = []
    while True:
        page = await session.list_threads_page(cursor=cursor, limit=200)
        values.extend(page.threads)
        if len(values) > MAX_PRODUCT_THREADS:
            raise ProductUIError("controller_thread_limit", "Workspace会话数量超过客户端上限")
        cursor = page.next_cursor
        if cursor is None:
            return _ordered_threads(tuple(values))
        if cursor in seen_cursors:
            raise ProductUIError("controller_pagination_stalled", "会话列表分页未推进")
        seen_cursors.add(cursor)


async def _select_thread(
    session: RecoverableAgentSession,
    available: tuple[ThreadView, ...],
    thread_id: UUID,
) -> ProductViewState:
    if thread_id not in {thread.thread_id for thread in available}:
        raise ProductUIError("controller_thread_missing", "所选会话不属于当前Workspace")
    await session.resume_thread(thread_id)
    return await session.hydrate_thread(thread_id)


async def _select_optional_thread(
    session: RecoverableAgentSession,
    available: tuple[ThreadView, ...],
    thread_id: UUID | None,
) -> ProductViewState | None:
    return None if thread_id is None else await _select_thread(session, available, thread_id)
