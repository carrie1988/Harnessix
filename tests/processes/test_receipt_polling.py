"""Owner回执的轮询间隔（ADR 0168）：从1毫秒起加倍到原固定间隔，启动等待的总时限与原来相当。"""

from __future__ import annotations

from itertools import islice

import pytest

from harnessix.processes.receipt_polling import receipt_poll_delays


def test_delays_double_from_one_millisecond_up_to_the_former_fixed_interval() -> None:
    assert list(islice(receipt_poll_delays(0.02), 8)) == [
        0.001,
        0.002,
        0.004,
        0.008,
        0.016,
        0.02,
        0.02,
        0.02,
    ]
    assert list(islice(receipt_poll_delays(0.01), 6)) == [0.001, 0.002, 0.004, 0.008, 0.01, 0.01]


def test_the_launch_wait_keeps_five_hundred_polls_and_about_five_seconds() -> None:
    delays = list(receipt_poll_delays(0.01, limit=500))
    assert len(delays) == 500
    # 原为500次×10毫秒；前几次更短，总等待只少25毫秒。
    assert sum(delays) == pytest.approx(5.0 - 0.025)


def test_a_zero_limit_yields_nothing() -> None:
    assert list(receipt_poll_delays(0.02, limit=0)) == []
