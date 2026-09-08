from __future__ import annotations

from collections.abc import Sequence

from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import (
    ExecutionPolicyBinding,
    SandboxBindingV2,
    SecretVersionBinding,
)
from harnessix.trusted_actions.contracts import (
    CanonicalActionResource,
    TrustedToolBinding,
)


class DefaultCodingRiskPolicy:
    """仅基于宿主绑定和规范资源决策，不读取调用方自报风险。"""

    version = "trusted-coding-action/v1"

    def evaluate(
        self,
        binding: TrustedToolBinding,
        resources: Sequence[CanonicalActionResource],
        sandbox: SandboxBindingV2,
        secrets: Sequence[SecretVersionBinding],
    ) -> ExecutionPolicyBinding:
        kinds = {item.kind for item in resources}
        accesses = {item.access for item in resources}
        has_effect_resource = bool(
            kinds & {"process", "network", "secret"}
            or accesses & {"write", "execute", "connect", "use", "update"}
        )
        if (
            binding.effect_class is EffectClass.DESTRUCTIVE
            or binding.risk_level is RiskLevel.CRITICAL
        ):
            return self._decision(
                PolicyDecisionKind.DENY,
                "default.deny-critical",
                "critical_or_destructive",
            )
        if binding.effect_class is EffectClass.READ_ONLY and has_effect_resource:
            return self._decision(
                PolicyDecisionKind.DENY,
                "default.deny-effect-mismatch",
                "readonly_resource_mismatch",
            )
        if binding.effect_class is not EffectClass.READ_ONLY and not has_effect_resource:
            return self._decision(
                PolicyDecisionKind.DENY,
                "default.deny-resource-missing",
                "write_resource_missing",
            )
        if ("network" in kinds) != (sandbox.network != "none"):
            return self._decision(
                PolicyDecisionKind.DENY,
                "default.deny-network-mismatch",
                "network_resource_mismatch",
            )
        if ("secret" in kinds) != bool(secrets):
            return self._decision(
                PolicyDecisionKind.DENY,
                "default.deny-secret-mismatch",
                "secret_resource_mismatch",
            )
        if (
            binding.effect_class is not EffectClass.READ_ONLY
            or binding.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}
            or sandbox.network != "none"
            or secrets
            or binding.recovery_mode == "external_reconcile"
        ):
            return self._decision(
                PolicyDecisionKind.REQUIRE_APPROVAL,
                "default.approve-canonical-effect",
                "canonical_effect_requires_approval",
            )
        return self._decision(
            PolicyDecisionKind.ALLOW,
            "default.allow-bounded-read",
            "bounded_read",
        )

    def _decision(
        self,
        decision: PolicyDecisionKind,
        policy_id: str,
        reason_code: str,
    ) -> ExecutionPolicyBinding:
        return ExecutionPolicyBinding(
            version=self.version,
            decision=decision,
            policy_id=policy_id,
            reason_code=reason_code,
        )
