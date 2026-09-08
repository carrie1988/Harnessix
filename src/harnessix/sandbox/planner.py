from __future__ import annotations

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.sandbox.contracts import (
    ContainerCommandSpec,
    ContainerSandboxProfile,
    NetworkPolicySnapshot,
    SandboxResourceLimits,
    WorkspaceMountMode,
)


def build_container_command(argv: tuple[str, ...], *, profile_digest: str) -> ContainerCommandSpec:
    payload = {
        "spec_version": "harnessix.container-command/v1",
        "argv": list(argv),
        "profile_digest": profile_digest,
    }
    try:
        return ContainerCommandSpec(
            argv=argv,
            profile_digest=profile_digest,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("sandbox_command_invalid", "容器命令无效") from None


def build_container_sandbox_profile(
    *,
    image: str,
    workspace_mode: WorkspaceMountMode,
    network: NetworkPolicySnapshot,
    limits: SandboxResourceLimits | None = None,
    run_as: str = "65532:65532",
    egress_gateway_digest: str | None = None,
) -> ContainerSandboxProfile:
    selected_limits = limits or SandboxResourceLimits()
    payload = {
        "spec_version": "harnessix.container-sandbox-profile/v1",
        "image": image,
        "workspace_mode": workspace_mode,
        "run_as": run_as,
        "limits": selected_limits.model_dump(mode="json", warnings="error"),
        "network": network.model_dump(mode="json", warnings="error"),
        "egress_gateway_digest": egress_gateway_digest,
    }
    try:
        return ContainerSandboxProfile(
            image=image,
            workspace_mode=workspace_mode,
            run_as=run_as,
            limits=selected_limits,
            network=network,
            egress_gateway_digest=egress_gateway_digest,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("sandbox_profile_invalid", "容器Sandbox Profile无效") from None
