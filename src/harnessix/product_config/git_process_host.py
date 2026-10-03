"""Git IO显式借用原产品资源；引用组不产生计划批准或新的进程权限。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, ExecutionPlanV2
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.owner_protocol import OutputRedactionSource
from harnessix.processes.supervisor import (
    PosixProcessSupervisor,
    SupervisedProcess,
    WindowsProcessSupervisor,
)
from harnessix.product_config.state_owner import ProductStateOwner
from harnessix.sandbox.process_runtime import ProcessSupervisor
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.tools.runtime import _drain

if TYPE_CHECKING:
    from harnessix.product_config.git_delivery_process import PreparedGitProcess


@dataclass(frozen=True, slots=True)
class GitProcessRuntimeHost:
    """只持原实际资源引用；关闭和执行权限仍由原产品上下文与Plan负责。"""

    owner: ProductStateOwner
    supervisor: ProcessSupervisor
    plans: SQLiteExecutionPlanStore
    protection: SecretPublicationScope

    def checkpoint(self, state_root: Path) -> None:
        """不重开Store或Owner；拒绝不同地址、关闭资源及可替换保护来源。"""
        if (
            type(self.owner) is not ProductStateOwner
            or type(self.supervisor) not in {PosixProcessSupervisor, WindowsProcessSupervisor}
            or type(self.plans) is not SQLiteExecutionPlanStore
            or type(self.protection) is not SecretPublicationScope
            or self.owner.state_root != state_root
            or self.supervisor._root != state_root / "process-owner"
            or self.plans._path != state_root / "execution-plans.db"
            or self.supervisor._output_redaction is not self.protection
            or self.supervisor._closed
            or self.plans._closed
        ):
            raise KernelError("git_process_runtime_mismatch", "Git缺少原有效共享宿主")
        self.owner.require_ready(state_root)
        self.protection.output_redaction_values()


@asynccontextmanager
async def open_git_process_resources(
    state_root: Path,
    protection: OutputRedactionSource | None,
    host: GitProcessRuntimeHost | None,
) -> AsyncIterator[ProcessSupervisor]:
    """显式区分借用和拥有；借用窗口绝不创建、替换或关闭共享Supervisor。"""
    if host is not None:
        host.checkpoint(state_root)
        if protection is None or (
            protection.output_redaction_values() != host.protection.output_redaction_values()
        ):
            raise KernelError("git_process_runtime_mismatch", "Git保护快照与原宿主不一致")
        yield host.supervisor
        host.checkpoint(state_root)
    else:
        supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
        async with supervisor_type(
            state_root / "process-owner", output_redaction=protection
        ) as supervisor:
            yield supervisor


def save_git_process_plan(
    state_root: Path, plan: ExecutionPlanV2, host: GitProcessRuntimeHost | None
) -> None:
    """使用原不可变Plan合同；借用原连接，独立用途保留原短连接窗口。"""
    if host is not None:
        host.checkpoint(state_root)
        host.plans.save_plan(plan)
    else:
        with SQLiteExecutionPlanStore(state_root / "execution-plans.db") as plans:
            plans.save_plan(plan)


async def start_git_process(
    supervisor: ProcessSupervisor,
    prepared: PreparedGitProcess,
    plan: ExecutionPlanV2,
    checkpoint: ExecutionApprovalCheckpoint | None,
) -> SupervisedProcess:
    """启动交接异常只结算本次新登记句柄，不能关闭共享宿主或停止原有调用。"""
    return await _start_git_process(
        supervisor, prepared, plan, checkpoint, prepared.approval_arguments()
    )


async def _start_git_process(
    supervisor: ProcessSupervisor,
    prepared: PreparedGitProcess,
    plan: ExecutionPlanV2,
    checkpoint: ExecutionApprovalCheckpoint | None,
    arguments: dict[str, JsonValue],
) -> SupervisedProcess:
    """父取消不取消启动线程；先完成本次交接及停止，才允许调用方释放租约。"""
    starting = asyncio.create_task(
        _start_owned_git_process(supervisor, prepared, plan, checkpoint, arguments)
    )
    try:
        return await asyncio.shield(starting)
    except asyncio.CancelledError as original:
        # to_thread取消不能回收已送达的Owner；必须保留并等待原启动任务。
        await _drain(starting)
        if not starting.cancelled():
            # 启动任务的强未知及原异常组优先于外层取消，不能静默丢失。
            handle = starting.result()
            stopping = asyncio.create_task(_settle_git_start(handle, original))
            await _drain(stopping)
            stopping.result()
        raise


async def _start_owned_git_process(
    supervisor: ProcessSupervisor,
    prepared: PreparedGitProcess,
    plan: ExecutionPlanV2,
    checkpoint: ExecutionApprovalCheckpoint | None,
    arguments: dict[str, JsonValue],
) -> SupervisedProcess:
    """原启动和异常结算由同一托管任务持有；只处理本次新登记句柄。"""
    previous = supervisor._handles.get(prepared.spec.process_id)
    try:
        return await supervisor.start(
            plan,
            prepared.spec,
            prepared.capability,
            workspace=prepared.command.cwd,
            environment=dict(prepared.command.environment),
            checkpoint=checkpoint,
            intent_arguments=arguments,
        )
    except BaseException as original:
        current = supervisor._handles.get(prepared.spec.process_id)
        if current is not None and current is not previous:
            await _settle_git_start(current, original)
        raise


async def _settle_git_start(handle: SupervisedProcess, original: BaseException) -> None:
    """只结算已交接的本次Owner；未知终态不能冒充停止成功。"""
    from harnessix.product_config.git_material_process import settle_input_failure

    try:
        await settle_input_failure(handle, uncertain=False)
        await handle.aclose()
        if handle.lease.state not in {"exited", "failed"}:
            raise KernelError("git_process_unknown", "Git启动句柄未取得可验真停止终态")
    except BaseException as cleanup:
        cause = BaseExceptionGroup("Git启动及停止结算失败", [original, cleanup])
        raise KernelError("git_process_unknown", "Git启动停止无法完成验真；禁止自动重放") from cause
