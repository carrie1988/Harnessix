"""事件页被服务端以超限拒绝时缩小页长重读；终端界面与无人值守客户端共用。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from harnessix.sdk.errors import AgentSDKError

# 事件页超过协商的消息字节上限（response_too_large）或公开数据保护扫描的资源上限
# （public_output_limit）时，服务端拒绝整页；页长更小即可读出。
OVERSIZED_PAGE_CODES = frozenset({"response_too_large", "public_output_limit"})


async def read_event_page[PageT](
    fetch: Callable[[int], Awaitable[PageT]], limit: int
) -> tuple[PageT, int]:
    """按limit读一页，被以超限拒绝时减半重读；返回这一页和读出它所用的页长。

    调用方此后沿用返回的页长，不必每页从头试探。单个事件仍被拒绝、或其他错误，原样抛出。
    """
    while True:
        try:
            return await fetch(limit), limit
        except AgentSDKError as error:
            if error.code not in OVERSIZED_PAGE_CODES or limit == 1:
                raise
            limit //= 2
