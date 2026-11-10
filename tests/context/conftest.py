"""context测试共享夹具。"""

from __future__ import annotations

import pytest

import harnessix.agent.compaction_input_budget as compaction_input_budget
import harnessix.agent.runtime as agent_runtime
import harnessix.agent.turn_budget_compaction as turn_budget_compaction
import harnessix.context.compaction_projection as compaction_projection


@pytest.fixture
def long_summary_header(monkeypatch):
    """较长说明头使最短摘要投影超过最小合法预留，才能构造预留不足的边界。

    说明头按名导入各模块，逐模块替换。
    """
    header = "[" + "h" * 140 + "]\n"
    for module in (
        compaction_projection,
        compaction_input_budget,
        turn_budget_compaction,
        agent_runtime,
    ):
        monkeypatch.setattr(module, "SUMMARY_PROJECTION_HEADER", header)
    return header
