"""宿主短暂卡顿时重做一次内部核对（ADR 0177）。

历史Artifact核对（5秒）与Action输出投影（10秒）平时只需几毫秒，超时几乎都来自整机卡顿：内存耗尽、
长时间换页、调度停顿。两者都不改变已提交的事实——前者只读，后者按Router审计终态重建同一份输出，
发布同一Artifact时返回已有收据——所以超时后稍等再做；三次都超时才按原错误失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from harnessix.agent.cancellation import CancelToken

# 第一次超时后等1秒、第二次后等2秒；整机卡顿通常几秒内恢复，持续变慢仍在约20秒内报错。
STALL_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0)


async def retry_after_host_stall[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    stalled: Callable[[Exception], bool],
    token: CancelToken,
) -> T:
    """`stalled`认定的超时重做`attempt`；其他错误、取消与最后一次超时原样抛出。"""

    for delay in (*STALL_RETRY_DELAYS_SECONDS, None):
        try:
            return await attempt()
        except Exception as error:
            if delay is None or not stalled(error):
                raise
        await token.run(asyncio.sleep(delay))
    raise AssertionError("unreachable")
