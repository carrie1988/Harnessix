"""Sandbox与网络隔离：把已验证意图冻结为可执行且可重放的计划。"""

from __future__ import annotations

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.sandbox.contracts import (
    DEFAULT_CONTAINER_WORKSPACE,
    ContainerCommandSpec,
    ContainerExecutionSpec,
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


def build_container_execution(
    command: ContainerCommandSpec,
    process: ProcessSpec,
    *,
    owner_capability_digest: str,
) -> ContainerExecutionSpec:
    payload = {
        "spec_version": "harnessix.container-execution/v1",
        "command": command.model_dump(mode="json", warnings="error"),
        "process": process.model_dump(mode="json", warnings="error"),
        "owner_capability_digest": owner_capability_digest,
    }
    try:
        return ContainerExecutionSpec(
            command=command,
            process=process,
            owner_capability_digest=owner_capability_digest,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("sandbox_execution_invalid", "容器执行合同无效") from None


def build_container_sandbox_profile(
    *,
    image: str,
    workspace_mode: WorkspaceMountMode,
    network: NetworkPolicySnapshot,
    limits: SandboxResourceLimits | None = None,
    run_as: str = "65532:65532",
    egress_gateway_digest: str | None = None,
    task_dependencies: bool = False,
    workspace_path: str = DEFAULT_CONTAINER_WORKSPACE,
    environment: tuple[tuple[str, str], ...] = (),
) -> ContainerSandboxProfile:
    selected_limits = limits or SandboxResourceLimits()
    payload: dict[str, object] = {
        "spec_version": "harnessix.container-sandbox-profile/v1",
        "image": image,
        "workspace_mode": workspace_mode,
        "run_as": run_as,
        "limits": selected_limits.model_dump(mode="json", warnings="error"),
        "network": network.model_dump(mode="json", warnings="error"),
        "egress_gateway_digest": egress_gateway_digest,
    }
    if task_dependencies:
        payload["task_dependencies"] = task_dependencies
    if workspace_path != DEFAULT_CONTAINER_WORKSPACE:
        payload["workspace_path"] = workspace_path
    if environment:
        payload["environment"] = [list(item) for item in environment]
    try:
        return ContainerSandboxProfile(
            image=image,
            workspace_mode=workspace_mode,
            run_as=run_as,
            limits=selected_limits,
            network=network,
            egress_gateway_digest=egress_gateway_digest,
            task_dependencies=task_dependencies,
            workspace_path=workspace_path,
            environment=environment,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("sandbox_profile_invalid", "容器Sandbox Profile无效") from None
