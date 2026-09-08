from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, ExecutionPlanV2
from harnessix.processes.supervision_contracts import (
    ProcessLaunchBinding,
    ProcessLease,
    ProcessSpec,
)
from harnessix.processes.supervision_planner import (
    build_process_launch_binding,
    build_process_spec,
)
from harnessix.processes.supervisor import (
    PosixProcessSupervisor,
    SupervisedProcess,
    WindowsProcessSupervisor,
)
from harnessix.sandbox.container import ContainerCommandBuilder, PreparedContainerLaunch
from harnessix.sandbox.contracts import (
    ContainerExecutionSpec,
    ContainerSandboxProfile,
    ManagedEgressBinding,
)
from harnessix.secrets.provider import ResolvedSecretEnvironment
from harnessix.workspace.contracts import ResourceAccess

ProcessSupervisor = PosixProcessSupervisor | WindowsProcessSupervisor


@dataclass(frozen=True, slots=True)
class PreparedSupervisedContainer:
    launch: PreparedContainerLaunch = field(repr=False)
    process: ProcessSpec = field(repr=False)
    binding: ProcessLaunchBinding


class SupervisedContainerProcess:
    def __init__(
        self,
        process: SupervisedProcess,
        builder: ContainerCommandBuilder,
        execution: ContainerExecutionSpec,
        launch_argv: tuple[str, ...],
    ) -> None:
        self._process = process
        self._builder = builder
        self._execution = execution
        self._launch_argv = launch_argv
        self._cleanup_lock = asyncio.Lock()
        self._cleaned = False

    @property
    def lease(self) -> ProcessLease:
        return self._process.lease

    @property
    def launch_argv(self) -> tuple[str, ...]:
        return self._launch_argv

    async def refresh(self) -> ProcessLease:
        return await self._process.refresh()

    async def send_stdin(self, data: bytes) -> None:
        await self._process.send_stdin(data)

    async def close_stdin(self) -> None:
        await self._process.close_stdin()

    async def stop(self) -> None:
        await self._process.stop()

    async def output(self, stream: Literal["stdout", "stderr"]) -> bytes:
        return await self._process.output(stream)

    async def wait(self, cancel: CancelToken | None = None) -> ProcessLease:
        try:
            lease = await self._process.wait(cancel)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup())
            raise
        await self._cleanup()
        return lease

    async def aclose(self) -> None:
        await self._process.aclose()
        await self._cleanup()

    async def _cleanup(self) -> None:
        async with self._cleanup_lock:
            if self._cleaned:
                return
            await asyncio.to_thread(self._builder.cleanup_container, self._execution)
            self._cleaned = True


class ContainerProcessRuntime:
    """把不可变Container执行合同物化到同一Process owner生命周期。"""

    def __init__(
        self,
        builder: ContainerCommandBuilder,
        supervisor: ProcessSupervisor,
    ) -> None:
        self._builder = builder
        self._supervisor = supervisor

    def prepare(
        self,
        plan: ExecutionPlanV2,
        checkpoint: ExecutionApprovalCheckpoint | None,
        profile: ContainerSandboxProfile,
        execution: ContainerExecutionSpec,
        *,
        workspace: str | Path,
        environment: Mapping[str, str],
        secrets: ResolvedSecretEnvironment | None = None,
        external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
        egress: ManagedEgressBinding | None = None,
    ) -> PreparedSupervisedContainer:
        capability = self._supervisor.capability
        if execution.owner_capability_digest != capability.digest:
            raise KernelError("process_capability_mismatch", "Container未绑定当前Process owner能力")
        launch = self._builder.prepare(
            plan,
            checkpoint,
            profile,
            workspace=workspace,
            command=execution,
            environment=environment,
            secrets=secrets,
            external_roots=external_roots,
            egress=egress,
            reattest_network=True,
        )
        if (
            launch.plan_fingerprint != plan.fingerprint
            or launch.profile_digest != profile.digest
            or launch.execution_digest != execution.digest
            or launch.container_name != self._builder.container_name(execution)
        ):
            raise KernelError("sandbox_capability_mismatch", "Container物化结果与执行合同不一致")
        source = execution.process
        process = build_process_spec(
            invocation="argv",
            argv=launch.argv,
            terminal="pipe",
            stdin=source.stdin,
            lifecycle=source.lifecycle,
            timeout_seconds=source.timeout_seconds,
            output_bytes=source.output_bytes,
            input_bytes=source.input_bytes,
            columns=source.columns,
            rows=source.rows,
            process_id=source.process_id,
        )
        binding = build_process_launch_binding(
            plan,
            process,
            capability,
            kind="container",
            environment=dict(launch.base_environment),
        )
        return PreparedSupervisedContainer(launch=launch, process=process, binding=binding)

    async def start(
        self,
        plan: ExecutionPlanV2,
        checkpoint: ExecutionApprovalCheckpoint | None,
        profile: ContainerSandboxProfile,
        execution: ContainerExecutionSpec,
        *,
        workspace: str | Path,
        environment: Mapping[str, str],
        secrets: ResolvedSecretEnvironment | None = None,
        external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
        egress: ManagedEgressBinding | None = None,
    ) -> SupervisedContainerProcess:
        await asyncio.to_thread(self._builder.ensure_container_absent, execution)
        prepared = await asyncio.to_thread(
            self.prepare,
            plan,
            checkpoint,
            profile,
            execution,
            workspace=workspace,
            environment=environment,
            secrets=secrets,
            external_roots=external_roots,
            egress=egress,
        )
        try:
            process = await self._supervisor.start_prepared(
                plan,
                prepared.process,
                self._supervisor.capability,
                prepared.binding,
                workspace=workspace,
                environment=prepared.launch.base_environment,
                secrets=secrets,
                checkpoint=checkpoint,
            )
        except BaseException:
            await asyncio.to_thread(self._builder.cleanup_container, execution)
            raise
        return SupervisedContainerProcess(process, self._builder, execution, prepared.process.argv)

    async def run(
        self,
        plan: ExecutionPlanV2,
        checkpoint: ExecutionApprovalCheckpoint | None,
        profile: ContainerSandboxProfile,
        execution: ContainerExecutionSpec,
        *,
        workspace: str | Path,
        environment: Mapping[str, str],
        secrets: ResolvedSecretEnvironment | None = None,
        external_roots: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]] | None = None,
        egress: ManagedEgressBinding | None = None,
        cancel: CancelToken | None = None,
    ) -> ProcessLease:
        handle = await self.start(
            plan,
            checkpoint,
            profile,
            execution,
            workspace=workspace,
            environment=environment,
            secrets=secrets,
            external_roots=external_roots,
            egress=egress,
        )
        return await handle.wait(cancel)

    async def reconcile(self, execution: ContainerExecutionSpec) -> ProcessLease:
        lease = await self._supervisor.reconcile(execution.process.process_id)
        await asyncio.to_thread(self._builder.cleanup_container, execution)
        return lease
