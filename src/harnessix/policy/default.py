from __future__ import annotations

from harnessix.domain.models import (
    ActionSnapshot,
    EffectClass,
    PolicyDecision,
    PolicyDecisionKind,
    RiskLevel,
    ToolDescriptor,
)


class DefaultPolicyEngine:
    """MVP 默认策略；后续可由 OPA/Cedar 适配器替换。"""

    async def evaluate(self, action: ActionSnapshot, tool: ToolDescriptor) -> PolicyDecision:
        if tool.effect_class is EffectClass.DESTRUCTIVE or tool.risk_level is RiskLevel.CRITICAL:
            return PolicyDecision(
                kind=PolicyDecisionKind.DENY,
                policy_id="default.deny-critical",
                reason="默认策略拒绝 destructive 或 critical Action",
            )
        if tool.requires_approval or tool.effect_class is EffectClass.NON_IDEMPOTENT_WRITE:
            return PolicyDecision(
                kind=PolicyDecisionKind.REQUIRE_APPROVAL,
                policy_id="default.approve-write",
                reason="该 Action 的运行时工具定义要求人工审批",
            )
        return PolicyDecision(
            kind=PolicyDecisionKind.ALLOW,
            policy_id="default.allow-safe",
            reason="运行时工具定义允许直接执行",
        )
