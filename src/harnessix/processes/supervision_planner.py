"""受监督进程：绑定Process意图、平台能力与Execution Plan。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import ExecutionPlanV2, canonical_digest
from harnessix.execution.planner import bind_environment
from harnessix.processes.supervision_contracts import (
    ProcessCapabilityProbe,
    ProcessInput,
    ProcessInvocation,
    ProcessLaunchBinding,
    ProcessLaunchKind,
    ProcessLease,
    ProcessLifecycle,
    ProcessSpec,
    ProcessTerminal,
    empty_process_output,
)
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import PlatformKind


def build_process_spec(
    *,
    invocation: ProcessInvocation,
    argv: tuple[str, ...] = (),
    shell_source: str | None = None,
    terminal: ProcessTerminal = "pipe",
    stdin: ProcessInput = "closed",
    lifecycle: ProcessLifecycle = "foreground",
    timeout_seconds: float = 300.0,
    output_bytes: int = 8 * 1024 * 1024,
    input_bytes: int = 0,
    columns: int = 120,
    rows: int = 40,
    process_id: UUID | None = None,
) -> ProcessSpec:
    identifier = process_id or uuid4()
    payload = {
        "spec_version": "harnessix.process-spec/v1",
        "process_id": str(identifier),
        "invocation": invocation,
        "argv": list(argv),
        "shell_source": shell_source,
        "terminal": terminal,
        "stdin": stdin,
        "lifecycle": lifecycle,
        "timeout_seconds": float(timeout_seconds),
        "output_bytes": output_bytes,
        "input_bytes": input_bytes,
        "columns": columns,
        "rows": rows,
    }
    try:
        return ProcessSpec(
            process_id=identifier,
            invocation=invocation,
            argv=argv,
            shell_source=shell_source,
            terminal=terminal,
            stdin=stdin,
            lifecycle=lifecycle,
            timeout_seconds=timeout_seconds,
            output_bytes=output_bytes,
            input_bytes=input_bytes,
            columns=columns,
            rows=rows,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_spec_invalid", "ProcessSpec不符合契约") from None


def build_process_capability(
    *,
    platform: PlatformKind,
    supports_pty: bool,
    implementation_digest: Revision,
) -> ProcessCapabilityProbe:
    modes: tuple[ProcessInvocation, ...] = (
        ("argv", "cmd", "powershell") if platform == "windows" else ("argv", "posix_sh")
    )
    payload = {
        "spec_version": "harnessix.process-capability/v1",
        "platform": platform,
        "owner_backend": "windows_job_object" if platform == "windows" else "posix_session",
        "invocation_modes": list(modes),
        "supports_pipe": True,
        "supports_pty": supports_pty,
        "supports_background": True,
        "supports_process_tree": True,
        "atomic_containment": True,
        "implementation_digest": implementation_digest,
    }
    try:
        return ProcessCapabilityProbe(
            platform=platform,
            owner_backend="windows_job_object" if platform == "windows" else "posix_session",
            invocation_modes=modes,
            supports_pipe=True,
            supports_pty=supports_pty,
            supports_background=True,
            supports_process_tree=True,
            atomic_containment=True,
            implementation_digest=implementation_digest,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_capability_invalid", "Process能力证据无效") from None


def build_process_launch_binding(
    plan: ExecutionPlanV2,
    spec: ProcessSpec,
    capability: ProcessCapabilityProbe,
    *,
    kind: ProcessLaunchKind,
    environment: dict[str, str] | None = None,
) -> ProcessLaunchBinding:
    bound_environment = (
        plan.environment
        if environment is None
        else bind_environment(environment, platform=capability.platform)
    )
    if (
        plan.workspace.platform != capability.platform
        or (kind == "host" and plan.sandbox.level == "container_strong")
        or (kind == "container" and plan.sandbox.level != "container_strong")
        or (
            kind == "host"
            and (
                plan.intent.arguments != spec.model_dump(mode="json", warnings="error")
                or plan.capabilities.provider_evidence_digest != capability.digest
                or bound_environment != plan.environment
            )
        )
    ):
        raise KernelError("process_capability_mismatch", "Process启动绑定与Execution Plan不一致")
    payload = {
        "spec_version": "harnessix.process-launch-binding/v1",
        "kind": kind,
        "platform": capability.platform,
        "plan_fingerprint": plan.fingerprint,
        "intent_arguments_digest": canonical_digest(plan.intent.arguments),
        "process_spec_digest": spec.digest,
        "capability_digest": capability.digest,
        "environment": [item.model_dump(mode="json") for item in bound_environment],
    }
    try:
        return ProcessLaunchBinding(
            kind=kind,
            platform=capability.platform,
            plan_fingerprint=plan.fingerprint,
            intent_arguments_digest=canonical_digest(plan.intent.arguments),
            process_spec_digest=spec.digest,
            capability_digest=capability.digest,
            environment=bound_environment,
            digest=canonical_digest(payload),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_binding_invalid", "Process启动绑定不符合契约") from None


def prepare_process_lease(
    plan: ExecutionPlanV2,
    spec: ProcessSpec,
    capability: ProcessCapabilityProbe,
    *,
    binding: ProcessLaunchBinding | None = None,
    now: datetime | None = None,
    owner_token: Revision | None = None,
) -> ProcessLease:
    checked_now = now or datetime.now(UTC)
    if checked_now.tzinfo is None:
        raise KernelError("process_lease_invalid", "Process Lease时间必须包含时区")
    launch_binding = binding or build_process_launch_binding(plan, spec, capability, kind="host")
    if (
        launch_binding.plan_fingerprint != plan.fingerprint
        or launch_binding.intent_arguments_digest != canonical_digest(plan.intent.arguments)
        or launch_binding.process_spec_digest != spec.digest
        or launch_binding.capability_digest != capability.digest
        or launch_binding.platform != capability.platform
        or plan.workspace.platform != capability.platform
        or spec.invocation not in capability.invocation_modes
        or (spec.terminal == "pty" and not capability.supports_pty)
        or (spec.lifecycle == "background" and not capability.supports_background)
        or not capability.supports_process_tree
    ):
        raise KernelError("process_capability_mismatch", "ProcessSpec超出Execution Plan能力")
    try:
        token = owner_token or os.urandom(32).hex()
        return ProcessLease(
            process_id=spec.process_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            process_spec_digest=spec.digest,
            capability_digest=capability.digest,
            launch_binding_digest=launch_binding.digest,
            lifecycle=spec.lifecycle,
            state="prepared",
            sequence=0,
            owner_token=token,
            deadline=checked_now + timedelta(seconds=spec.timeout_seconds),
            stdout=empty_process_output(),
            stderr=empty_process_output(),
        )
    except (ValidationError, ValueError, TypeError):
        raise KernelError("process_lease_invalid", "Process Lease不符合契约") from None
