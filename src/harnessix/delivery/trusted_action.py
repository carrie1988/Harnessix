"""把多文件Workspace Patch绑定到Trusted Action与可恢复Delivery账本。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    PROTECTED_COMPONENTS,
    WorkspaceMutation,
    WorkspaceTransactionPlan,
    WorkspaceTransactionRecord,
)
from harnessix.delivery.diff import build_workspace_diff
from harnessix.delivery.filesystem import WorkspaceTransactionRuntime
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.trusted_action_contracts import (
    WorkspacePatchFile,
    WorkspacePatchInput,
    build_workspace_action_review,
)
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRoutePlan,
    CanonicalActionResource,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    canonical_action_resource,
)
from harnessix.workspace.contracts import (
    PlatformKind,
    WorkspaceResourceRequest,
)
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key

WORKSPACE_PATCH_TOOL = "apply_patch_batch"
WORKSPACE_PATCH_VERSION = "harnessix.workspace-patch/v1"
WORKSPACE_PATCH_EXECUTOR = "product.workspace-patch"
PRODUCT_ACTION_SOURCE = "harnessix.product"
WORKSPACE_PATCH_LEASE_SECONDS = 300.0

WorkspaceRootResolver = Callable[[str], Path]


def workspace_patch_descriptor() -> ToolDescriptor:
    """返回模型可见合同；执行、租约和恢复字段均由宿主持有。"""

    return ToolDescriptor(
        name=WORKSPACE_PATCH_TOOL,
        version=WORKSPACE_PATCH_VERSION,
        description=(
            "以一个需审批、可恢复的事务创建、替换或删除最多16个Workspace文本文件；"
            "replace/delete必须携带读取到的完整SHA-256前置条件。"
        ),
        input_schema=WorkspacePatchInput.model_json_schema(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def workspace_patch_executor_evidence() -> str:
    """绑定当前POSIX执行、成员级取消和只观察恢复语义。"""

    return canonical_digest(
        {
            "implementation": "workspace-transaction-runtime/v1",
            "action": WORKSPACE_PATCH_VERSION,
            "platform": "posix",
            "member_checkpoint": True,
            "reconcile_writes": False,
        }
    )


def build_workspace_patch_definition(
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
    workspace_root: WorkspaceRootResolver,
) -> TrustedActionDefinition:
    """从同一Descriptor构造Router Binding、资源解析器和Delivery Executor。"""

    descriptor = workspace_patch_descriptor()
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id=PRODUCT_ACTION_SOURCE,
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=descriptor.effect_class,
        risk_level=descriptor.risk_level,
        recovery_mode="durable_ledger",
        executor_id=WORKSPACE_PATCH_EXECUTOR,
    )

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        checked = _arguments(arguments)
        if context.cwd != ".":
            raise KernelError("workspace_patch_cwd_unsupported", "Workspace Patch只支持根级cwd")
        return resolve_workspace_patch(checked, context.capabilities.platform)

    return TrustedActionDefinition(
        binding=binding,
        input_model=WorkspacePatchInput,
        resolve=resolve,
        executor=WorkspacePatchActionExecutor(
            transactions,
            leases,
            workspace_root,
        ),
    )


def resolve_workspace_patch(
    proposal: WorkspacePatchInput,
    platform: PlatformKind,
) -> ResolvedAction:
    """按目标平台规范化路径，并绑定目标内容摘要、操作和所有父目录。"""

    normalized = _normalized_files(proposal, platform)
    resources: list[CanonicalActionResource] = []
    workspace: dict[tuple[str, str], WorkspaceResourceRequest] = {}
    for path, item in normalized:
        resources.append(_action_resource(path, item))
        workspace[(path, "write")] = WorkspaceResourceRequest(path=path, access="write")
        parts = path.split("/")[:-1]
        for index in range(len(parts) + 1):
            parent = "/".join(parts[:index]) or "."
            workspace[(parent, "read")] = WorkspaceResourceRequest(path=parent, access="read")
    return ResolvedAction(
        resources=tuple(resources),
        workspace_resources=tuple(workspace[key] for key in sorted(workspace)),
    )


class WorkspacePatchTransactionPlanner:
    """查询优先地把已冻结Route物化为同身份Delivery事务。"""

    def __init__(
        self,
        transactions: SQLiteWorkspaceTransactionStore,
        workspace_root: WorkspaceRootResolver,
    ) -> None:
        self._transactions = transactions
        self._workspace_root = workspace_root

    @property
    def transactions(self) -> SQLiteWorkspaceTransactionStore:
        return self._transactions

    def prepare(
        self,
        route: ActionRoutePlan,
        proposal: WorkspacePatchInput,
    ) -> WorkspaceTransactionRecord:
        """复用精确计划；不存在时才捕获来源、验证前置条件并保存Blob。"""

        checked = _validate_route_intent(route, proposal)
        try:
            existing = self._transactions.load(route.execution.plan_id)
        except KernelError as error:
            if error.code != "delivery_transaction_not_found":
                raise
        else:
            _validate_transaction(route, checked, existing.plan)
            return existing
        root = self._workspace_root(route.execution.workspace.workspace_id)
        prepared = prepare_workspace_transaction(
            root,
            _desired_files(checked, route.execution.workspace.platform),
            request_id=_request_id(route.execution.plan_id),
            transaction_id=route.execution.plan_id,
            platform=route.execution.workspace.platform,
        )
        _validate_transaction(route, checked, prepared.plan)
        saved = self._transactions.save(prepared)
        _validate_transaction(route, checked, saved.plan)
        return saved

    def load(
        self,
        route: ActionRoutePlan,
        proposal: WorkspacePatchInput,
    ) -> WorkspaceTransactionRecord:
        checked = _validate_route_intent(route, proposal)
        record = self._transactions.load(route.execution.plan_id)
        _validate_transaction(route, checked, record.plan)
        return record


class WorkspacePatchActionExecutor:
    """以Workspace Lease逐成员发布；取消后由Router转UNKNOWN且不自动续写。"""

    def __init__(
        self,
        transactions: SQLiteWorkspaceTransactionStore,
        leases: WorkspaceLeaseStore,
        workspace_root: WorkspaceRootResolver,
        *,
        lease_seconds: float = WORKSPACE_PATCH_LEASE_SECONDS,
    ) -> None:
        if type(lease_seconds) not in {int, float} or not 0 < lease_seconds <= 3600:
            raise ValueError("Workspace Patch租约时长无效")
        self._planner = WorkspacePatchTransactionPlanner(transactions, workspace_root)
        self._runtime = WorkspaceTransactionRuntime(transactions, leases)
        self._leases = leases
        self._workspace_root = workspace_root
        self._lease_seconds = float(lease_seconds)

    async def execute(
        self,
        plan: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        proposal = _arguments(arguments)
        record = self._planner.load(plan, proposal)
        root = self._workspace_root(plan.execution.workspace.workspace_id)
        lease = self._leases.acquire(
            record.plan.source.workspace_id,
            _lease_owner(plan.execution.plan_id),
            ttl_seconds=self._lease_seconds,
        )
        try:
            while record.state != "published":
                # Task取消只在尚未开始下一个原子成员时生效；已进入的单成员必须完成记账。
                await asyncio.sleep(0)
                record = self._runtime.publish_next(
                    record.transaction_id,
                    root,
                    approval_fingerprint=record.plan.fingerprint,
                    lease=lease,
                )
        finally:
            self._leases.release(lease)
        return self._outcome(record, origin="execution")

    async def reconcile(
        self,
        plan: ActionRoutePlan,
        arguments: BaseModel,
    ) -> ActionExecutionOutcome:
        proposal = _arguments(arguments)
        try:
            record = self._planner.load(plan, proposal)
        except KernelError as error:
            if error.code == "delivery_transaction_not_found":
                return ActionExecutionOutcome(
                    kind="manual_intervention",
                    error_code="delivery_plan_missing",
                )
            raise
        root = self._workspace_root(plan.execution.workspace.workspace_id)
        record = self._runtime.reconcile(record.transaction_id, root)
        return self._outcome(record, origin="recovery")

    def _outcome(
        self,
        record: WorkspaceTransactionRecord,
        *,
        origin: str,
    ) -> ActionExecutionOutcome:
        if record.state == "published":
            diff = build_workspace_diff(record.plan, self._planner.transactions)
            review = build_workspace_action_review(record.plan, diff.entries, diff.text)
            return ActionExecutionOutcome(
                kind="succeeded",
                output={
                    "transaction_id": str(record.transaction_id),
                    "files": len(record.plan.mutations),
                    "state": record.state,
                    "origin": origin,
                    "diff_sha256": diff.sha256,
                },
                artifact_sha256=hashlib.sha256(review.to_jsonl()).hexdigest(),
            )
        if record.state == "prepared" or (record.state == "interrupted" and record.cursor == 0):
            return ActionExecutionOutcome(kind="failed", error_code="delivery_not_applied")
        if origin == "execution":
            return ActionExecutionOutcome(kind="unknown", error_code="delivery_effect_unknown")
        return ActionExecutionOutcome(
            kind="manual_intervention",
            error_code=(
                "delivery_partial_effect"
                if record.state == "interrupted"
                else record.error_code or "delivery_effect_unknown"
            ),
        )


def _arguments(arguments: BaseModel) -> WorkspacePatchInput:
    try:
        return WorkspacePatchInput.model_validate_json(arguments.model_dump_json())
    except ValueError:
        raise KernelError(
            "workspace_patch_arguments_invalid", "Workspace Patch参数类型不一致"
        ) from None


def _normalized_files(
    proposal: WorkspacePatchInput,
    platform: PlatformKind,
) -> tuple[tuple[str, WorkspacePatchFile], ...]:
    normalized: list[tuple[str, WorkspacePatchFile]] = []
    keys: set[str] = set()
    for item in proposal.files:
        path = normalize_workspace_path(item.path, platform)
        key = path_comparison_key(path, platform)
        if path == "." or key in keys or _protected(path):
            raise KernelError("delivery_path_denied", "Workspace Patch路径不允许写入")
        keys.add(key)
        normalized.append((path, item.model_copy(update={"path": path})))
    return tuple(sorted(normalized, key=lambda pair: path_comparison_key(pair[0], platform)))


def _desired_files(
    proposal: WorkspacePatchInput,
    platform: PlatformKind,
) -> dict[str, DesiredWorkspaceFile]:
    return {
        path: DesiredWorkspaceFile(
            None if item.operation == "delete" else (item.content or "").encode("utf-8"),
            item.mode,
        )
        for path, item in _normalized_files(proposal, platform)
    }


def _action_resource(path: str, item: WorkspacePatchFile) -> CanonicalActionResource:
    after_sha256 = (
        hashlib.sha256((item.content or "").encode("utf-8")).hexdigest()
        if item.operation != "delete"
        else None
    )
    return canonical_action_resource(
        kind="workspace",
        access="write",
        identifier={"location": "workspace", "path": path},
        attributes={
            "operation": item.operation,
            "expected_sha256": item.expected_sha256,
            "after_sha256": after_sha256,
            "mode": item.mode,
        },
    )


def _validate_route_intent(
    route: ActionRoutePlan,
    proposal: WorkspacePatchInput,
) -> WorkspacePatchInput:
    checked = WorkspacePatchInput.model_validate(proposal)
    expected_resources = tuple(
        sorted(
            resolve_workspace_patch(checked, route.execution.workspace.platform).resources,
            key=lambda item: (
                item.kind,
                item.access,
                item.identifier_sha256,
                item.attributes_sha256,
            ),
        )
    )
    if (
        route.binding.tool != WORKSPACE_PATCH_TOOL
        or route.binding.executor_id != WORKSPACE_PATCH_EXECUTOR
        or route.execution.plan_id != route.invocation.invocation_id
        or route.invocation.arguments != checked.model_dump(mode="json")
        or route.resources != expected_resources
    ):
        raise KernelError("delivery_action_mismatch", "Delivery事务与Action Route不一致")
    return checked


def _validate_transaction(
    route: ActionRoutePlan,
    proposal: WorkspacePatchInput,
    transaction: WorkspaceTransactionPlan,
) -> None:
    files = _normalized_files(proposal, transaction.source.platform)
    expected = {path: item for path, item in files}
    if (
        transaction.transaction_id != route.execution.plan_id
        or transaction.request_id != _request_id(route.execution.plan_id)
        or transaction.source != route.execution.workspace
        or tuple(item.path for item in transaction.mutations) != tuple(expected)
    ):
        raise KernelError("delivery_action_mismatch", "Delivery计划未绑定原Action计划")
    for mutation in transaction.mutations:
        _validate_mutation(expected[mutation.path], mutation)


def _validate_mutation(item: WorkspacePatchFile, mutation: WorkspaceMutation) -> None:
    if item.operation == "create":
        before_valid = mutation.before.presence == "absent"
    else:
        before_valid = (
            mutation.before.presence == "file" and mutation.before.sha256 == item.expected_sha256
        )
    if item.operation == "delete":
        after_valid = mutation.after.presence == "absent"
    else:
        expected_body = (item.content or "").encode("utf-8")
        after_valid = (
            mutation.after.presence == "file"
            and mutation.after.sha256 == hashlib.sha256(expected_body).hexdigest()
            and mutation.after.size == len(expected_body)
            and mutation.after.mode == item.mode
        )
    if not before_valid or not after_valid:
        raise KernelError("workspace_patch_precondition_failed", "Workspace Patch前置条件不成立")


def _request_id(plan_id: UUID) -> str:
    return f"action:{plan_id}"


def _lease_owner(plan_id: UUID) -> str:
    return f"workspace-patch:{plan_id}"


def _protected(path: str) -> bool:
    return any(
        component.casefold() in PROTECTED_COMPONENTS or component.casefold().startswith(".env")
        for component in path.split("/")
    )


def workspace_patch_supported() -> bool:
    """只有具备no-follow目录句柄语义的本机POSIX端口可以被广告。"""

    return os.name == "posix" and all(
        hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    )
