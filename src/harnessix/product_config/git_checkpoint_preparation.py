"""原Agent可信准备入口的真实Checkpoint规划；不发布A/D或签发Git执行批准。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.agent.reducer import get_turn
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.delivery.git_inventory_wire import _wire
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.git_tree_diff import GitTreeDiff
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_baseline import _BASELINE_TIMEOUT_SECONDS
from harnessix.product_config.git_checkpoint_materials import (
    collect_product_git_checkpoint_materials,
)
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryCoreV2
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitCheckpointInput,
    ProductGitWorktreeIntent,
    product_git_delivery_resource,
)
from harnessix.product_config.git_delivery_plan_materials import (
    read_product_git_delivery_core_materials_v2,
)
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot
from harnessix.product_config.git_delivery_process import (
    GitDeliveryProcess,
    GitOperationBudget,
    PreparedGitProcess,
)
from harnessix.product_config.git_delivery_source import verify_git_delivery_source
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.product_config.git_repository_observation import GitRepositoryReadAuthorization
from harnessix.product_config.git_user_authority import require_git_user_authority
from harnessix.product_config.git_user_observation import (
    _native_checkpointer,
    _verify_observed_git_state,
    collect_product_git_user_observation,
)
from harnessix.product_config.git_user_observation_contracts import ProductGitUserObservation
from harnessix.product_config.git_user_observation_paths import (
    PinnedGitUserDirectories,
    pin_git_user_directories,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.agent_gateway_invocation import build_agent_action_invocation
from harnessix.trusted_actions.contracts import CodingActionInvocation, TrustedToolBinding
from harnessix.trusted_actions.planning import external_action_identity
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionRouter,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


def _invalid() -> KernelError:
    """固定规划失败不包含作者、配置、对象正文或内部路径。"""
    return KernelError("git_checkpoint_preparation_invalid", "Git规划缺少同一原认证调用或宿主")


class ProductGitCheckpointPreparer:
    """受信装配只注册原prepare接口；已有Route重试由原Gateway先查询而非重规划。"""

    def __init__(
        self,
        session: SQLiteSessionStore,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        reader: GitReadRuntime,
        material_port: GitDeliveryProcess,
        worktree_parent: Path,
        *,
        binding: TrustedToolBinding,
        authorize: Callable[[PreparedGitProcess], Awaitable[GitRepositoryReadAuthorization]],
        limits: GitTreeClosureLimits,
        max_parents: int,
        snapshot_ports: WorkspaceSnapshotPorts,
    ) -> None:
        self.session, self.router, self.core_store, self.reader = (
            session,
            router,
            core_store,
            reader,
        )
        self.material_port, self.worktree_parent = material_port, worktree_parent
        self.binding, self.authorize = binding, authorize
        self.limits, self.max_parents, self.ports = limits, max_parents, snapshot_ports

    async def prepare(
        self,
        invocation: CodingActionInvocation,
        arguments: BaseModel,
        context: ActionPlanningContext,
        thread: Thread,
        turn: Turn,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> ResolvedAction:
        """单次原60秒包括认证、全部对象采集、规范Core持久与末端事实复核。"""
        return await _prepare_entry(
            self, invocation, arguments, context, thread, turn, call, cancel
        )


def _original_preparation_fields(
    planner: ProductGitCheckpointPreparer, original: tuple[tuple[str, object], ...]
) -> bool:
    """只比较确切原字典快照，拒绝字典／键子类借 copy、比较或查找执行回调。"""
    if type(planner) is not ProductGitCheckpointPreparer:
        return False
    current = object.__getattribute__(planner, "__dict__")
    if type(current) is not dict:
        return False
    fields = tuple(current.items())
    if any(type(name) is not str for name, _ in fields):
        return False
    current = dict(fields)
    return (
        all(type(name) is str for name, _ in original)
        and tuple(current) == tuple(name for name, _ in original)
        and all(current[name] is value for name, value in original)
    )


def _preparation_control(
    planner: ProductGitCheckpointPreparer,
    context: ActionPlanningContext,
    cancel: CancelToken,
    budget: GitOperationBudget,
    failures: list[BaseException],
) -> tuple[Callable[[], None], Callable[[], None]]:
    """冻结原资源，返回纯段频检与完整认证；两者借用同一期限和父取消。"""
    original = tuple(vars(planner).items())
    core_store, material_port, session = planner.core_store, planner.material_port, planner.session
    resource_types = tuple(map(type, (core_store, material_port, session)))
    transactions = planner.core_store.store
    implementation = git_checkpoint_preparation_implementation_digest()
    authority = require_git_user_authority(
        planner.session, planner.router, transactions, planner.ports, planner.reader
    )
    host = planner.material_port._runtime_host
    if type(host) is not GitProcessRuntimeHost:
        raise _invalid()
    host_references = (
        host.owner,
        host.supervisor,
        host.plans,
        host.protection,
        planner.material_port._runner,
    )
    owner = planner.session._runtime_owner_token

    def control() -> None:
        try:
            cancel.checkpoint()
            budget.remaining()
            if context.checkpoint is not None:
                context.checkpoint()
            authority()
            if planner.core_store.store is not transactions or planner.material_port._closed:
                raise _invalid()
            current_host = (
                host.owner,
                host.supervisor,
                host.plans,
                host.protection,
                planner.material_port._runner,
            )
            if any(a is not b for a, b in zip(current_host, host_references, strict=True)):
                raise _invalid()
            if git_checkpoint_preparation_implementation_digest() != implementation:
                raise _invalid()
            if tuple(vars(planner).keys()) != tuple(name for name, _ in original) or any(
                vars(planner)[name] is not value for name, value in original
            ):
                raise _invalid()
            _verify_host(planner, host, owner, context)
        except BaseException as error:
            failures.append(error)
            raise

    def local_check() -> None:
        """同步纯算法只比较原内存引用；不调用来源读器、Owner或外部回调。"""
        try:
            cancel.checkpoint()
            budget.remaining()
            # 先比对一次内存快照，再读冻结的原对象；替换代理不能借属性执行回调。
            if not _original_preparation_fields(planner, original):
                raise _invalid()
            if (
                any(
                    type(resource) is not expected
                    for resource, expected in zip(
                        (core_store, material_port, session), resource_types, strict=True
                    )
                )
                or type(host) is not GitProcessRuntimeHost
                or getattr(core_store, "store", None) is not transactions
                or getattr(material_port, "_closed", None) is not False
                or getattr(material_port, "_runtime_host", None) is not host
                or getattr(session, "_runtime_owner_token", None) is not owner
                or any(
                    actual is not expected
                    for actual, expected in zip(
                        (
                            getattr(host, "owner", None),
                            getattr(host, "supervisor", None),
                            getattr(host, "plans", None),
                            getattr(host, "protection", None),
                            getattr(material_port, "_runner", None),
                        ),
                        host_references,
                        strict=True,
                    )
                )
            ):
                raise _invalid()
        except BaseException as error:
            failures.append(error)
            raise

    return parent_cancel_checkpointer(local_check), parent_cancel_checkpointer(control)


async def _prepare_entry(
    planner: ProductGitCheckpointPreparer,
    invocation: CodingActionInvocation,
    arguments: BaseModel,
    context: ActionPlanningContext,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
) -> ResolvedAction:
    """唯一期限与托管收尾入口；保留原Owner未知结算及原检查点异常身份。"""
    await asyncio.sleep(0)
    if type(cancel) is not CancelToken or type(context) is not ActionPlanningContext:
        raise _invalid()
    if (
        type(planner.core_store) is not ProductGitDeliveryCoreStore
        or type(planner.material_port) is not GitDeliveryProcess
    ):
        raise _invalid()
    budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
    failures: list[BaseException] = []
    local_check, check = _preparation_control(planner, context, cancel, budget, failures)
    check()

    async def prepare_original() -> ResolvedAction:
        # 在实际受管子Task内创建纯段控制，但认证闭包仍冻结入口的原资源。
        control = (
            GitAuthenticationControl(local_check, check)
            if type(planner) is ProductGitCheckpointPreparer
            else check
        )
        return await _prepare(
            planner, invocation, arguments, context, thread, turn, call, cancel, budget, control
        )

    try:
        async with asyncio.timeout(budget.remaining()):
            result = await cancel.run(
                prepare_original(),
                preserve_failure=True,
            )
            check()
            return result
    except TimeoutError as error:
        if any(error is failure for failure in failures):
            raise
        raise KernelError("git_process_timeout", "Git规划总期限已耗尽") from None
    except UpstreamCheckpointError as error:
        raise error.error from None


def _verify_host(
    planner: ProductGitCheckpointPreparer,
    host: object,
    owner: object,
    context: ActionPlanningContext,
) -> None:
    """原Root、Session、PlanStore、Scope及工作区上下文必须同属一个实际宿主。"""
    publication = planner.session._publication
    if (
        type(planner.core_store) is not ProductGitDeliveryCoreStore
        or type(planner.material_port) is not GitDeliveryProcess
        or type(host) is not GitProcessRuntimeHost
        or publication is None
        or owner is None
        or planner.session._runtime_owner_token is not owner
        or planner.material_port._runtime_host is not host
        or host.plans is not planner.router._plans
        or host.protection is not publication._events._protection
        or planner.material_port._state != planner.session.path.parent
        or planner.material_port._output_redaction is not host.protection
        or context.workspace_root != planner.reader._root
        or context.cwd != "."
        or context.external_roots not in (None, {})
        or context.snapshot_ports is not planner.ports
        or type(planner.limits) is not GitTreeClosureLimits
        or type(planner.max_parents) is not int
        or planner.max_parents < 0
    ):
        raise _invalid()
    host.checkpoint(planner.session.path.parent)


async def _history(
    planner: ProductGitCheckpointPreparer,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    expected: AuthenticatedThreadHistory | None = None,
) -> AuthenticatedThreadHistory:
    """首次和末段都读取实际完整MAC历史，不以传入Thread或调用UUID认定归属。"""
    history = await planner.session.authenticated_thread_history(
        thread.thread_id, cancel=cancel, deadline=budget._deadline, checkpoint=check
    )
    check()
    if (
        history.thread != thread
        or get_turn(history.thread, turn.turn_id) != turn
        or (expected is not None and history != expected)
    ):
        raise _invalid()
    ToolExecutionScope.for_pending_call(history.thread, turn.turn_id, call)
    return history


async def _prepare(
    planner: ProductGitCheckpointPreparer,
    invocation: CodingActionInvocation,
    arguments: BaseModel,
    context: ActionPlanningContext,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
) -> ResolvedAction:
    """原认证调用到真实完整材料的纵向链；CAS孤儿不是业务成功或执行权限。"""
    thread, turn, call = (
        _snapshot(thread, Thread, check),
        _snapshot(turn, Turn, check),
        _snapshot(call, ToolCallContent, check),
    )
    arguments = _snapshot(arguments, ProductGitCheckpointInput, check)
    invocation = _snapshot(invocation, CodingActionInvocation, check)
    binding = _snapshot(planner.binding, TrustedToolBinding, check)
    definition = planner.router._definition(binding.source, binding.source_id, binding.tool)
    if (
        definition.agent_prepare is not planner
        or definition.binding != binding
        or call.tool != "git_checkpoint"
        or invocation
        != build_agent_action_invocation(thread, turn, call, binding, requires_idempotency=True)
        or arguments.model_dump(mode="json") != call.arguments
    ):
        raise _invalid()
    history = await _history(planner, thread, turn, call, cancel, budget, check)
    observation = await collect_product_git_user_observation(
        thread,
        arguments.patches,
        planner.router,
        planner.core_store.store,
        planner.reader,
        session=planner.session,
        cancel=cancel,
        budget=budget,
        checkpoint=check,
        snapshot_ports=planner.ports,
    )
    parent = planner.worktree_parent
    if not parent.is_absolute() or not parent.is_relative_to(planner.session.path.parent):
        raise _invalid()
    with pin_git_user_directories(parent, parent, checkpoint=check) as pinned_parent:
        scope, diff = await collect_product_git_checkpoint_materials(
            planner.material_port,
            context.workspace_root,
            observation.baseline,
            GitMaterialCAS(planner.core_store.store),
            authorize=planner.authorize,
            limits=planner.limits,
            max_parents=planner.max_parents,
            cancel=cancel,
            budget=budget,
            checkpoint=check,
        )
        check()
        intents = _capture_intents(parent, pinned_parent, observation, scope, check)
        core = _persist_core(
            planner,
            invocation,
            binding,
            thread,
            turn,
            call,
            observation,
            intents,
            scope,
            diff,
            check,
        )
        await _verify_observation(
            planner, observation, thread, turn, call, cancel, budget, check, history
        )
        _verify_intents(pinned_parent, intents, check)
    return ResolvedAction(
        resources=(product_git_delivery_resource(core),),
        workspace_resources=tuple(
            WorkspaceResourceRequest(location=r.location, path=r.path, access=r.access)
            for r in core.baseline.source.workspace.resources
        ),
        expected_workspace=core.baseline.source.workspace,
    )


def _capture_intents(
    parent: Path,
    pinned_parent: PinnedGitUserDirectories,
    observation: ProductGitUserObservation,
    scope: GitInventoryScope,
    check: Callable[[], None],
) -> tuple[ProductGitWorktreeIntent, ...]:
    """冻结原物理父目录及两新UUID缺失观察；不创建工作树或授权发布。"""
    roles: tuple[Literal["anchor", "delivery"], ...] = ("anchor", "delivery")
    intents = tuple(
        ProductGitWorktreeIntent(
            role=role,
            worktree_id=uuid4(),
            parent_path=str(parent),
            parent_identity=pinned_parent.common_identity,
            platform=scope.platform,
            base_commit_oid=observation.baseline.head_oid,
        )
        for role in roles
    )
    _verify_intents(pinned_parent, intents, check)
    return intents


def _verify_intents(
    pinned_parent: PinnedGitUserDirectories,
    intents: tuple[ProductGitWorktreeIntent, ...],
    check: Callable[[], None],
) -> None:
    """首末均观察原UUID仍缺失；父目录身份不变不代表子项仍不存在。"""
    pinned_parent.verify(check)
    for intent in intents:
        if (
            pinned_parent.index.observe(
                str(intent.worktree_id), access="read", checkpoint=_native_checkpointer(check)
            ).kind
            != "missing"
        ):
            raise _invalid()
    check()


def _persist_core(
    planner: ProductGitCheckpointPreparer,
    invocation: CodingActionInvocation,
    binding: TrustedToolBinding,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    observation: ProductGitUserObservation,
    intents: tuple[ProductGitWorktreeIntent, ...],
    scope: GitInventoryScope,
    diff: GitTreeDiff,
    check: Callable[[], None],
) -> ProductGitDeliveryCoreV2:
    """全部已验证事实按原字段规范封签；原CAS耐久回读前不返回新资源。"""
    payload = {
        "spec_version": "harnessix.product-git-delivery-core/v2",
        "delivery_id": str(external_action_identity(invocation, binding)),
        "store_id": str(observation.store_id),
        "key_id": str(observation.key_id),
        "thread_id": str(thread.thread_id),
        "turn_id": str(turn.turn_id),
        "call": call.model_dump(mode="json"),
        "user_observation": observation.model_dump(mode="json"),
        "anchor_intent": intents[0].model_dump(mode="json"),
        "worktree_intent": intents[1].model_dump(mode="json"),
        "checkpoint_delivery_id": None,
        "object_scope": _wire(scope, check, native_fields=True),
        "diff_sha256": diff.content.sha256,
        "diff_bytes": diff.content.utf8_bytes,
        "commit_spec": None,
        "implementation_digest": git_checkpoint_preparation_implementation_digest(),
    }
    payload["fingerprint"] = canonical_digest(payload)
    try:
        core = ProductGitDeliveryCoreV2.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            strict=True,
            context={"checkpoint": check},
        )
    except ValidationError:
        raise _invalid() from None
    core = planner.core_store.persist_v2(core, checkpoint=check)
    read_product_git_delivery_core_materials_v2(
        GitMaterialCAS(planner.core_store.store), core, checkpoint=check
    )
    return core


async def _verify_observation(
    planner: ProductGitCheckpointPreparer,
    observation: ProductGitUserObservation,
    thread: Thread,
    turn: Turn,
    call: ToolCallContent,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    history: AuthenticatedThreadHistory,
) -> None:
    """同次末段复核物理目录、配置、逻辑Index、认证历史和当前完整Source。"""

    async def verify_history() -> None:
        await _history(planner, thread, turn, call, cancel, budget, check, history)

    def verify_source() -> None:
        try:
            verify_git_delivery_source(
                thread,
                observation.baseline.source,
                planner.router,
                planner.core_store.store,
                checkpoint=_native_checkpointer(check),
                snapshot_ports=planner.ports,
            )
        except UpstreamCheckpointError as error:
            raise error.error from None

    await _verify_observed_git_state(
        observation,
        planner.reader,
        cancel,
        check,
        verify_history=verify_history,
        verify_source=verify_source,
        invalid=_invalid,
    )


# 只冻结不变的字段名和安装位置；每个控制点仍重读四份源码完整字节。
# 不以 mtime/size 缓存 SHA，也不缓存文件正文，保持同元数据篡改可被发现。
_PREPARATION_SOURCE_PATHS = tuple(
    (str(Path(name)), Path(__file__).parent.parent / name)
    for name in (
        "product_config/git_checkpoint_preparation.py",
        "product_config/git_checkpoint_materials.py",
        "product_config/git_checkpoint_scope.py",
        "delivery/git_inventory_wire.py",
    )
)


def git_checkpoint_preparation_implementation_digest() -> str:
    """按原四字段重读完整规划配方；热检查点不重复构造固定路径。"""
    try:
        return canonical_digest(
            {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in _PREPARATION_SOURCE_PATHS
            }
        )
    except OSError:
        raise _invalid() from None
