"""Owner回执的轮询间隔（ADR 0168）：从1毫秒起逐次加倍，到调用方原来的固定间隔为止。

简单命令的回执在几毫秒内出现，固定间隔会让每条命令平均白等半个间隔；运行较久的命令几次之后
就回到原来的间隔，轮询只多出几次。非职责：不读取回执、不决定何时停止轮询。
"""

from __future__ import annotations

from collections.abc import Iterator

FIRST_POLL_SECONDS = 0.001


def receipt_poll_delays(ceiling: float, *, limit: int | None = None) -> Iterator[float]:
    """1、2、4……毫秒，每项不超过ceiling，之后一直是ceiling；给出limit时共limit项，否则无限。"""
    delay, count = FIRST_POLL_SECONDS, 0
    while limit is None or count < limit:
        yield min(delay, ceiling)
        delay, count = min(delay * 2, ceiling), count + 1
