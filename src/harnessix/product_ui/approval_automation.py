"""审批自动化：本会话规则只保存在内存，重启即失效；沙箱外命令另有工作区规则（ADR 0128）。

修改类审批可整体自动接受；其他审批只允许与用户批准过的调用完全相同的调用。沙箱外命令先看内置
只读命令和用户保存的“以后不再询问”前缀（见approval_rules），这些规则跨会话保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import UUID

from harnessix.product_config.command_rules import rule_label
from harnessix.product_config.wrap_up import WRAP_UP_TEXTS
from harnessix.product_ui.approval_rules import CommandRules, host_command
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_service import InteractionService
from harnessix.product_ui.interactions import (
    PendingApproval,
    RespondApprovalIntent,
    approval_review,
    pending_approval,
)
from harnessix.product_ui.projection import ProductViewState
from harnessix.protocol.contracts import PublicTextContent

EDIT_APPROVAL_TYPES: Final = frozenset({"patch", "patch_batch"})
AUTO_APPROVAL_REASON: Final = "用户已选择在本会话自动批准此类操作"
WRAP_UP_REJECTION_REASON: Final = "你已要求收尾，这次操作不再执行"


@dataclass(slots=True)
class ApprovalAutomation:
    """记录本会话的自动批准规则，以及已经尝试过的审批，避免对同一审批重复尝试。"""

    auto_accept_edits: bool = False
    command_rules: CommandRules | None = None
    _allowed_exact: set[tuple[str, str]] = field(default_factory=set)
    _attempted: set[UUID] = field(default_factory=set)

    def rule_offer(self, pending: PendingApproval) -> str | None:
        """批准时可以保存的“以后不再询问”规则文案；没有可保存的规则时返回None。"""

        prefix = self._offer(pending)
        return None if prefix is None else rule_label(prefix)

    def remember(self, pending: PendingApproval) -> None:
        """修改类整体自动接受；命令能保存前缀规则时长期保存，否则只记住本会话的相同调用。"""

        prefix = self._offer(pending)
        if pending.approval_type in EDIT_APPROVAL_TYPES:
            self.auto_accept_edits = True
        elif prefix is not None and self.command_rules is not None:
            self.command_rules.remember(prefix)
        else:
            self._allowed_exact.add((pending.tool, pending.arguments_json))

    def reason(self, pending: PendingApproval) -> str | None:
        """规则允许自动批准时返回写入审批记录的原因；否则返回None，留给用户。"""

        if pending.approval_type in EDIT_APPROVAL_TYPES:
            return AUTO_APPROVAL_REASON if self.auto_accept_edits else None
        if (pending.tool, pending.arguments_json) in self._allowed_exact:
            return AUTO_APPROVAL_REASON
        cmd = host_command(pending)
        if cmd is None or self.command_rules is None:
            return None
        return self.command_rules.reason(cmd)

    def allows(self, pending: PendingApproval) -> bool:
        return self.reason(pending) is not None

    def _offer(self, pending: PendingApproval) -> tuple[str, ...] | None:
        cmd = host_command(pending)
        if cmd is None or self.command_rules is None:
            return None
        return self.command_rules.offer(cmd)

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
    # 收尾之后还在等审批的操作直接拒绝（Beta Issue #4）：Ctrl+F时调用已经提出、审批窗口还没弹出。
    wrap_up = wrap_up_requested(view)
    reason = None if pending is None else automation.reason(pending)
    reason = WRAP_UP_REJECTION_REASON if wrap_up and pending is not None else reason
    if pending is None or reason is None or not automation.claim_attempt(pending):
        return None
    evidence = await service.load_approval_evidence(view, pending.binding)
    if not wrap_up and not approval_review(view, evidence).approve_allowed:
        return None
    outcome: Literal["approved", "rejected"] = "rejected" if wrap_up else "approved"
    return await service.respond_approval(
        view, evidence, RespondApprovalIntent(pending.binding, outcome=outcome, reason=reason)
    )


def wrap_up_requested(view: ProductViewState | None) -> bool:
    """当前这一轮是否已记入收尾说明（与宿主的判定同一份文本）。"""

    if view is None or view.current_turn is None:
        return False
    turn = view.current_turn
    return any(
        entry.turn_id == turn.turn_id
        and isinstance(entry.item.content, PublicTextContent)
        and entry.item.content.kind == "user_message"
        and entry.item.content.text in WRAP_UP_TEXTS
        for entry in view.items
    )
