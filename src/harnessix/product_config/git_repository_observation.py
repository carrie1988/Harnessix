"""借原产品宿主执行共享Git仓库读取；不创建授权、不启动同步或后台副作用。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.delivery.git import git_delivery_implementation_digest
from harnessix.delivery.git_contracts import GitRepositoryBinding
from harnessix.delivery.git_identity import _executable_identity
from harnessix.delivery.git_repository_recipe import repository_binding_recipe
from harnessix.execution.contracts import ExecutionApprovalCheckpoint, ExecutionPlanV2
from harnessix.product_config.git_delivery_process import (
    GitDeliveryProcess,
    GitOperationBudget,
    PreparedGitProcess,
)
from harnessix.product_config.git_process_host import GitProcessRuntimeHost


@dataclass(frozen=True, slots=True)
class GitRepositoryReadAuthorization:
    """由受信宿主提供的原正式命令计划；数据本身不增授权限。"""

    plan: ExecutionPlanV2
    approval: ExecutionApprovalCheckpoint | None


async def _authorize_read(
    authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]],
    prepared: PreparedGitProcess,
    cancel: CancelToken,
    budget: GitOperationBudget,
) -> GitRepositoryReadAuthorization:
    """等待计划也受同一取消与绝对期限约束，不留下无界后台准备任务。"""
    cancel.checkpoint()
    try:
        async with asyncio.timeout(budget.remaining()):
            result = await cancel.run(authorize(prepared))
    except TimeoutError:
        raise KernelError("git_process_timeout", "Git操作总期限已耗尽") from None
    if type(result) is not GitRepositoryReadAuthorization:
        raise KernelError("git_process_plan_mismatch", "Git读取缺少原正式命令计划")
    return result


async def observe_product_git_repository(
    port: GitDeliveryProcess,
    root: Path,
    workspace_id: str,
    *,
    authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]],
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
) -> GitRepositoryBinding:
    """原共享Owner下串行验真固定读取；成功只表示仓库绑定，不表示Git交付。"""
    cancel.checkpoint()
    budget.remaining()
    # 先交付调用方已待处理的Task取消，再冻结本次操作的新增取消计数。
    await asyncio.sleep(0)
    host, runner, state = port._runtime_host, port._runner, port._state
    if type(host) is not GitProcessRuntimeHost:
        raise KernelError("git_process_runtime_mismatch", "Git读取必须借用原产品宿主")
    identity = runner.identity
    implementation = git_delivery_implementation_digest()

    def raw_check() -> None:
        cancel.checkpoint()
        budget.remaining()
        checkpoint()
        if port._closed:
            raise KernelError("git_process_closed", "Git受控端口已关闭")
        if (
            port._runtime_host is not host
            or port._runner is not runner
            or port._state != state
            or port._output_redaction is not host.protection
            or runner.identity != identity
            or _executable_identity(runner.path) != identity
        ):
            raise KernelError("git_process_runtime_mismatch", "Git读取原宿主身份已经变化")
        host.checkpoint(state)

    check = parent_cancel_checkpointer(raw_check)
    check()
    recipe = repository_binding_recipe(
        root,
        workspace_id,
        platform="windows" if os.name == "nt" else "posix",
        executable_identity=identity,
        implementation_digest=implementation,
        checkpoint=check,
    )
    try:
        request = next(recipe)
        while True:
            check()
            prepared = port.prepare(
                request.cwd, request.arguments, budget=budget, accepted=request.accepted
            )
            authorization = await _authorize_read(authorize, prepared, cancel, budget)
            check()
            completion = await port.run(
                prepared,
                authorization.plan,
                cancel,
                budget=budget,
                checkpoint=authorization.approval,
            )
            check()
            try:
                request = recipe.send(completion.stdout)
            except StopIteration as completed:
                result = cast(GitRepositoryBinding, completed.value)
                break
        check()
        if git_delivery_implementation_digest() != implementation:
            raise KernelError("git_repository_changed", "Git仓库绑定已经变化")
        return result
    finally:
        recipe.close()
