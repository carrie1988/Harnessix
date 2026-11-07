"""宿主直接执行的Trusted Action共用执行器：Eval测试Profile与原生命令Profile（ADR 0121）。

调用方提供参数校验、Route核对与ProcessSpec派生；本模块只负责经POSIX Process Owner执行、
从账本对账，以及把终态转换为Router可持久化的结果和与审计摘要绑定的输出。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.supervision_contracts import ProcessLease, ProcessSpec
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.trusted_output import (
    TrustedProcessOutputDocument,
    build_trusted_process_output,
)
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    ActionRouteSnapshot,
)
from harnessix.trusted_actions.router import TrustedActionRouter

# 输出Artifact ID的命名空间；沿用Eval测试Profile的原值，已发布的Artifact ID保持不变。
_ACTION_OUTPUT_NAMESPACE = UUID("4c575932-3fba-4cc6-8bc8-7cdd84796b93")
_STOP_REASON_CODES = {
    "timeout": "process_timeout",
    "cancelled": "process_cancelled",
    "output_limit": "process_output_limit",
    "input_limit": "process_input_limit",
    "io_error": "process_io_error",
    "closed": "process_closed",
    "cleanup_failed": "process_cleanup_failed",
    "host_lost": "process_host_lost",
    "unknown": "process_state_unknown",
    "launch_failed": "process_launch_failed",
}
# 启动前即可确定失败、不会产生副作用的错误码。
_PREFLIGHT_CODES = frozenset(
    {
        "approval_required",
        "execution_plan_stale",
        "process_capability_mismatch",
        "secret_binding_mismatch",
        "workspace_snapshot_changed",
    }
)


@dataclass(frozen=True, slots=True)
class HostProcessBinding:
    """一类宿主进程Action的固定执行绑定。

    nonzero_exit_fails：命令类工具把非零退出码报为失败；测试类工具把它当作需反馈给模型的事实。
    """

    label: str
    supervisor: PosixProcessSupervisor
    environment: Mapping[str, str]
    check_arguments: Callable[[BaseModel], BaseModel]
    validate_route: Callable[[ActionRoutePlan, BaseModel], None]
    process_spec: Callable[[ActionRoutePlan, BaseModel], ProcessSpec]
    workspace: Callable[[ActionRoutePlan], Path]
    public_output: Callable[[TrustedProcessOutputDocument], dict[str, JsonValue]]
    nonzero_exit_fails: bool
    preflight_codes: frozenset[str] = frozenset()


async def _output_document(
    binding: HostProcessBinding, plan_id: UUID
) -> TrustedProcessOutputDocument:
    lease = binding.supervisor.status(plan_id)
    if lease.state not in {"exited", "failed", "unknown"}:
        raise KernelError("process_not_terminal", "宿主Process尚未形成终态")
    stdout, stderr = await asyncio.gather(
        binding.supervisor.output(plan_id, "stdout"),
        binding.supervisor.output(plan_id, "stderr"),
    )
    try:
        return build_trusted_process_output(binding.label, lease, stdout, stderr)
    except ValueError:
        raise KernelError("process_output_corrupt", "宿主Process输出与Lease不一致") from None


async def _lease_outcome(
    binding: HostProcessBinding,
    lease: ProcessLease,
    *,
    origin: Literal["execution", "recovery"],
) -> ActionExecutionOutcome:
    try:
        document = await _output_document(binding, lease.process_id)
    except KernelError:
        return ActionExecutionOutcome(
            kind="unknown" if origin == "execution" else "manual_intervention",
            error_code="process_output_unavailable",
        )
    kind: Literal["succeeded", "failed", "unknown", "manual_intervention"]
    if lease.state == "unknown":
        kind = "unknown" if origin == "execution" else "manual_intervention"
        error_code: str | None = "process_state_unknown"
    elif lease.state == "failed":
        kind, error_code = "failed", "process_launch_failed"
    elif lease.stop_reason == "exited" and (
        lease.returncode == 0 or not binding.nonzero_exit_fails
    ):
        kind, error_code = "succeeded", None
    elif lease.stop_reason == "exited":
        kind, error_code = "failed", "process_nonzero_exit"
    else:
        kind = "failed"
        error_code = _STOP_REASON_CODES.get(lease.stop_reason or "unknown", "process_failed")
    body = document.to_jsonl()
    return ActionExecutionOutcome(
        kind=kind,
        output=binding.public_output(document),
        artifact_sha256=hashlib.sha256(body).hexdigest(),
        error_code=error_code,
    )


async def _execution_failure(
    binding: HostProcessBinding,
    plan_id: UUID,
    error: KernelError,
) -> ActionExecutionOutcome:
    """根据可证明的Lease存在性区分前置失败与副作用不确定。"""

    try:
        lease = binding.supervisor.status(plan_id)
    except KernelError as missing:
        if missing.code != "process_lease_not_found":
            return ActionExecutionOutcome(kind="unknown", error_code="process_state_unavailable")
        deterministic = error.code in _PREFLIGHT_CODES | binding.preflight_codes
        return ActionExecutionOutcome(
            kind="failed" if deterministic else "unknown",
            error_code="process_preflight_failed" if deterministic else "process_effect_unknown",
        )
    return await _lease_outcome(binding, lease, origin="execution")


class HostProcessActionExecutor:
    """从批准的参数确定性派生ProcessSpec，只经POSIX Process Owner执行。"""

    def __init__(self, binding: HostProcessBinding, router: TrustedActionRouter) -> None:
        self.binding = binding
        self._router = router

    async def execute(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        binding = self.binding
        checked = binding.check_arguments(arguments)
        binding.validate_route(route, checked)
        spec = binding.process_spec(route, checked)
        checkpoint = self._router.approval(route.execution.plan_id)
        try:
            lease = await binding.supervisor.run(
                route.execution,
                spec,
                binding.supervisor.capability,
                workspace=binding.workspace(route),
                environment=binding.environment,
                checkpoint=checkpoint,
                intent_arguments=route.invocation.arguments,
            )
        except asyncio.CancelledError:
            raise
        except KernelError as error:
            return await _execution_failure(binding, route.execution.plan_id, error)
        return await _lease_outcome(binding, lease, origin="execution")

    async def reconcile(
        self,
        route: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        checked = self.binding.check_arguments(arguments)
        self.binding.validate_route(route, checked)
        try:
            lease = await self.binding.supervisor.reconcile(route.execution.plan_id)
        except KernelError as error:
            if error.code == "process_lease_not_found":
                return ActionExecutionOutcome(kind="failed", error_code="process_not_started")
            return ActionExecutionOutcome(
                kind="manual_intervention",
                error_code="process_reconciliation_failed",
            )
        return await _lease_outcome(self.binding, lease, origin="recovery")

    async def output_document(self, plan_id: UUID) -> TrustedProcessOutputDocument:
        return await _output_document(self.binding, plan_id)


class HostProcessOutputProvider:
    """从Supervisor账本重建正文，发布与Router摘要绑定的Action Output。"""

    def __init__(
        self,
        executor: HostProcessActionExecutor,
        artifacts: SQLiteArtifactStore,
        *,
        workspace_scope: str,
    ) -> None:
        self._executor = executor
        self._artifacts = artifacts
        self._workspace_scope = workspace_scope

    async def output(
        self,
        route: ActionRouteSnapshot,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        *,
        expected_output_sha256: str,
        expected_artifact_sha256: str,
        cancel: CancelToken,
    ) -> JsonValue:
        cancel.checkpoint()
        document = await self._executor.output_document(route.plan.execution.plan_id)
        body = document.to_jsonl()
        public = self._executor.binding.public_output(document)
        if (
            canonical_digest(public) != expected_output_sha256
            or hashlib.sha256(body).hexdigest() != expected_artifact_sha256
        ):
            raise KernelError("trusted_action_output_mismatch", "宿主Process输出与Router审计不一致")
        reference = await self._artifacts.publish_action_output(
            thread.thread_id,
            turn.turn_id,
            call,
            body,
            artifact_id=uuid5(_ACTION_OUTPUT_NAMESPACE, str(route.plan.execution.plan_id)),
            workspace_scope=self._workspace_scope,
            expected_sequence=thread.sequence,
        )
        return cast(JsonValue, {**public, "artifact": reference.model_dump(mode="json")})
