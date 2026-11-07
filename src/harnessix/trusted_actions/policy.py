"""统一可信Action路由：评估统一Action的资源、风险与审批要求。"""

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

# 本机沙箱内的命令自动执行，越出沙箱才询问（ADR 0113第2.1节）。
# Agent内核要求写调用成功前必有已批准的审批项，所以这类命令仍走审批，但以专门的策略ID标记，
# 由宿主以系统身份立即批准；其余写调用的人工审批不变。
_NATIVE_SANDBOX_BACKENDS = frozenset({"seatbelt", "bubblewrap"})
_SANDBOXED_COMMAND_RESOURCES = frozenset({"process", "workspace", "external", "network"})
_COMMAND_EXECUTOR_PREFIX = "product.command-profile."
SANDBOXED_COMMAND_POLICY_ID = "default.approve-sandboxed-command"


class DefaultCodingRiskPolicy:
    """仅基于宿主绑定和规范资源决策，不读取调用方自报风险。

    v2只新增“本机沙箱内的命令可由宿主自动批准”一条标记规则，其余判定与v1相同；
    已记录的v1计划保持原决策。
    """

    version = "trusted-coding-action/v2"

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
        if _sandboxed_command(binding, resources, sandbox, secrets):
            return self._decision(
                PolicyDecisionKind.REQUIRE_APPROVAL,
                SANDBOXED_COMMAND_POLICY_ID,
                "sandboxed_command",
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


def _sandboxed_command(
    binding: TrustedToolBinding,
    resources: Sequence[CanonicalActionResource],
    sandbox: SandboxBindingV2,
    secrets: Sequence[SecretVersionBinding],
) -> bool:
    """命令执行器在已证明的本机沙箱内运行，且只声明沙箱边界内的资源、不注入Secret。"""

    return (
        sandbox.level == "host_sandboxed"
        and sandbox.backend in _NATIVE_SANDBOX_BACKENDS
        and binding.executor_id.startswith(_COMMAND_EXECUTOR_PREFIX)
        and not secrets
        and {item.kind for item in resources} <= _SANDBOXED_COMMAND_RESOURCES
        and all(item.kind != "network" or item.access == "connect" for item in resources)
    )
