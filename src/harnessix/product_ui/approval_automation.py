"""本会话的审批自动化：只在用户明确选择后生效，只保存在内存，重启即失效。

修改类审批可整体自动接受；命令等其他审批只允许与用户批准过的调用完全相同的调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_service import InteractionService
from harnessix.product_ui.interactions import (
    PendingApproval,
    RespondApprovalIntent,
    approval_review,
    pending_approval,
)
from harnessix.product_ui.projection import ProductViewState

EDIT_APPROVAL_TYPES: Final = frozenset({"patch", "patch_batch"})
AUTO_APPROVAL_REASON: Final = "用户已选择在本会话自动批准此类操作"


@dataclass(slots=True)
class ApprovalAutomation:
    """记录本会话的自动批准规则，以及已经尝试过的审批，避免对同一审批重复尝试。"""

    auto_accept_edits: bool = False
    _allowed_exact: set[tuple[str, str]] = field(default_factory=set)
    _attempted: set[UUID] = field(default_factory=set)

    def remember(self, pending: PendingApproval) -> None:
        if pending.approval_type in EDIT_APPROVAL_TYPES:
            self.auto_accept_edits = True
        else:
            self._allowed_exact.add((pending.tool, pending.arguments_json))

    def allows(self, pending: PendingApproval) -> bool:
        if pending.approval_type in EDIT_APPROVAL_TYPES:
            return self.auto_accept_edits
        return (pending.tool, pending.arguments_json) in self._allowed_exact

    def claim_attempt(self, pending: PendingApproval) -> bool:
        """同一审批只自动尝试一次；失败后交还给用户人工处理。"""

        if pending.binding.approval_id in self._attempted:
            return False
        self._attempted.add(pending.binding.approval_id)
        return True


async def auto_approve_pending(
    service: InteractionService,
    view: ProductViewState | None,
    automation: ApprovalAutomation,
) -> ProductViewState | None:
    """规则命中且证据完整时批准当前审批并返回新视图；否则返回None，留给人工处理。"""

    try:
        pending = pending_approval(view)
    except ProductUIError:
        # 事件分页间的瞬时状态，下一轮轮询再判断，不打扰用户。
        return None
    if pending is None or not automation.allows(pending) or not automation.claim_attempt(pending):
        return None
    evidence = await service.load_approval_evidence(view, pending.binding)
    if not approval_review(view, evidence).approve_allowed:
        return None
    return await service.respond_approval(
        view,
        evidence,
        RespondApprovalIntent(pending.binding, outcome="approved", reason=AUTO_APPROVAL_REASON),
    )
