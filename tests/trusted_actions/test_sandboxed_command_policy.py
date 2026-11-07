"""策略v2：本机沙箱内的命令标记为可由宿主自动批准，其余判定与v1相同（ADR 0113第2.1节）。"""

from __future__ import annotations

from typing import Any

import pytest

from harnessix.domain.models import EffectClass, PolicyDecisionKind, RiskLevel
from harnessix.execution.contracts import SandboxBindingV2, SecretVersionBinding
from harnessix.trusted_actions.contracts import build_trusted_tool_binding
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.router import canonical_action_resource


def _binding(executor_id: str = "product.command-profile.shell", **overrides: Any):
    values: dict[str, Any] = {
        "source": "builtin",
        "source_id": "harnessix.product",
        "tool": "exec_command.shell",
        "tool_version": "1",
        "tool_fingerprint": "a" * 64,
        "input_schema_sha256": "b" * 64,
        "effect_class": EffectClass.NON_IDEMPOTENT_WRITE,
        "risk_level": RiskLevel.HIGH,
        "recovery_mode": "durable_ledger",
        "executor_id": executor_id,
    }
    return build_trusted_tool_binding(**(values | overrides))


def _sandbox(level: str = "host_sandboxed", backend: str = "bubblewrap", network: str = "none"):
    return SandboxBindingV2(
        level=level,  # type: ignore[arg-type]
        backend=backend,
        backend_version="1",
        network=network,  # type: ignore[arg-type]
        capability_digest="c" * 64,
        profile_digest="d" * 64,
    )


def _resources(*, network: bool = False, extra: tuple[str, ...] = ()):
    items = [
        canonical_action_resource(
            kind="process", access="execute", identifier={"profile": "shell"}
        ),
        canonical_action_resource(
            kind="workspace", access="write", identifier={"location": "workspace", "path": "."}
        ),
        canonical_action_resource(
            kind="external",
            access="write",
            identifier={"location": "native-write-root", "path": "/c"},
        ),
    ]
    if network:
        items.append(
            canonical_action_resource(
                kind="network", access="connect", identifier={"policy": "full"}
            )
        )
    for kind in extra:
        items.append(canonical_action_resource(kind=kind, access="update", identifier={"x": kind}))  # type: ignore[arg-type]
    return tuple(items)


def test_version_is_bumped_for_the_new_rule() -> None:
    assert DefaultCodingRiskPolicy.version == "trusted-coding-action/v2"


@pytest.mark.parametrize(("backend", "network"), [("bubblewrap", "none"), ("seatbelt", "full")])
def test_sandboxed_commands_are_marked_for_automatic_approval(backend: str, network: str) -> None:
    decision = DefaultCodingRiskPolicy().evaluate(
        _binding(),
        _resources(network=network != "none"),
        _sandbox(backend=backend, network=network),
        (),
    )
    # 内核要求写调用先有批准记录，所以仍是需要审批，由宿主按策略ID自动批准。
    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert (decision.policy_id, decision.reason_code) == (
        "default.approve-sandboxed-command",
        "sandboxed_command",
    )


@pytest.mark.parametrize(
    "case",
    [
        {
            "sandbox": {"level": "host_guarded", "backend": "host", "network": "full"},
            "network": True,
        },
        {"sandbox": {"level": "container_strong", "backend": "docker"}},
        {"executor": "product.eval-test.run"},
        {"extra": ("git_ref",)},
        {"secrets": True},
    ],
)
def test_everything_else_keeps_manual_approval(case: dict[str, Any]) -> None:
    secrets = (
        (SecretVersionBinding(name="token", version="1", target="TOKEN"),)
        if case.get("secrets")
        else ()
    )
    resources = _resources(network=case.get("network", False), extra=case.get("extra", ()))
    if secrets:
        resources = (
            *resources,
            canonical_action_resource(kind="secret", access="use", identifier={"name": "token"}),
        )
    decision = DefaultCodingRiskPolicy().evaluate(
        _binding(case.get("executor", "product.command-profile.shell")),
        resources,
        _sandbox(**case.get("sandbox", {})),
        secrets,
    )
    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert decision.policy_id == "default.approve-canonical-effect"


def test_destructive_actions_are_still_denied_inside_the_sandbox() -> None:
    decision = DefaultCodingRiskPolicy().evaluate(
        _binding(effect_class=EffectClass.DESTRUCTIVE),
        _resources(),
        _sandbox(),
        (),
    )
    assert decision.decision is PolicyDecisionKind.DENY
