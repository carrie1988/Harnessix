"""哪些user_message是Harnessix自动发出的说明（ADR 0173，Beta Issue #46）。

宿主说明落为普通的用户消息Item（模型历史里它们就是用户消息），身份由各自的模块按Turn与步骤
派生、重放确定。这里汇总全部派生规则，按身份判定，不看正文前缀：用户自己输入同样的文字仍是用户。
旧会话的说明身份规则相同，升级后导出与事件同样能区分。新增一种宿主说明时须在这里登记
（`tests/agent/test_host_notice_identity.py`核对每个调用`host_notice_events`的模块都已登记）。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from uuid import UUID

from harnessix.agent.cancelled_turn_notice import cancelled_turn_notice_item_id
from harnessix.agent.completion_guard import completion_notice_item_id
from harnessix.agent.request_notes import BudgetLevel, budget_note_item_id, catalog_note_item_id
from harnessix.agent.session_baseline import baseline_loss_item_id, session_baseline_item_id
from harnessix.agent.step_notices import step_notice_item_id
from harnessix.agent.truncation_recovery import (
    recovery_notice_item_id,
    truncated_call_notice_item_id,
)

# Budget.max_steps的上限：每步至多一条的说明按步骤（或序号）派生，至多这么多条。
MAX_NOTICE_STEPS = 1000
_PER_STEP: tuple[Callable[[UUID, int], UUID], ...] = (
    completion_notice_item_id,
    recovery_notice_item_id,
    truncated_call_notice_item_id,
    step_notice_item_id,
    catalog_note_item_id,
)
_PER_TURN: tuple[Callable[[UUID], UUID], ...] = (
    session_baseline_item_id,
    baseline_loss_item_id,
    cancelled_turn_notice_item_id,
)


def is_host_notice(turn_id: UUID | None, item_id: UUID) -> bool:
    """这条Item是不是本Turn里Harnessix自动发出的说明。"""
    return turn_id is not None and item_id in _notice_ids(turn_id)


@lru_cache(maxsize=256)
def _notice_ids(turn_id: UUID) -> frozenset[UUID]:
    per_step = (
        derive(turn_id, step) for derive in _PER_STEP for step in range(MAX_NOTICE_STEPS + 1)
    )
    per_turn = (derive(turn_id) for derive in _PER_TURN)
    budget = (budget_note_item_id(turn_id, level) for level in BudgetLevel)
    return frozenset((*per_step, *per_turn, *budget))
