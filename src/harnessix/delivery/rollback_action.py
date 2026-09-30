"""产品Patch回滚：用原after作前置条件、原before作目标，保存独立逆向事务。"""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import BaseModel, ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import WorkspaceTransactionPlan, WorkspaceTransactionRecord
from harnessix.delivery.planner import DesiredWorkspaceFile, prepare_workspace_transaction
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.transaction_action_executor import WorkspaceTransactionActionExecutor
from harnessix.delivery.trusted_action import (
    PRODUCT_ACTION_SOURCE,
    WorkspaceRootResolver,
)
from harnessix.domain.models import ContractModel, EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import (
    ActionRoutePlan,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    canonical_action_resource,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.leases import WorkspaceLeaseStore
from harnessix.workspace.snapshot import capture_workspace_snapshot

WORKSPACE_ROLLBACK_TOOL = "rollback_workspace_patch"
WORKSPACE_ROLLBACK_VERSION = "harnessix.workspace-patch-rollback/v1"
WORKSPACE_ROLLBACK_EXECUTOR = "product.workspace-patch-rollback"


class WorkspaceRollbackInput(ContractModel):
    """仅指定本会话成功Patch事务；根、文件目标和批准由宿主决定。"""

    transaction_id: UUID


def decode_workspace_rollback_input(value: object) -> WorkspaceRollbackInput:
    """复用严格JSON入口，拒绝未知字段、非UUID及用户提供的执行权限。"""

    try:
        return WorkspaceRollbackInput.model_validate_json(json.dumps(value, allow_nan=False))
    except (ValidationError, ValueError, TypeError):
        raise KernelError(
            "workspace_rollback_arguments_invalid", "Patch回滚参数不符合契约"
        ) from None


def workspace_rollback_descriptor() -> ToolDescriptor:
    """返回需要全新批准、冲突拒绝且可恢复的产品回滚工具。"""

    return ToolDescriptor(
        name=WORKSPACE_ROLLBACK_TOOL,
        version=WORKSPACE_ROLLBACK_VERSION,
        description="回滚本会话一次成功的apply_patch_batch；transaction_id使用该调用返回的UUID。"
        "生成完整逆向Diff并重新审批；当前文件必须仍与原修改后版本一致，后续改动不会被覆盖。",
        input_schema=WorkspaceRollbackInput.model_json_schema(),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def workspace_rollback_binding() -> TrustedToolBinding:
    """Doctor和运行时从同一Descriptor捕获不可伪造的宿主Binding。"""

    descriptor = workspace_rollback_descriptor()
    return build_trusted_tool_binding(
        source="builtin",
        source_id=PRODUCT_ACTION_SOURCE,
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=descriptor.effect_class,
        risk_level=descriptor.risk_level,
        recovery_mode="durable_ledger",
        executor_id=WORKSPACE_ROLLBACK_EXECUTOR,
    )


def _original(
    transactions: SQLiteWorkspaceTransactionStore, transaction_id: UUID, workspace_id: str
) -> WorkspaceTransactionRecord:
    record = transactions.load(transaction_id)
    if record.state != "published" or record.plan.source.workspace_id != workspace_id:
        raise KernelError("workspace_rollback_source_invalid", "Patch回滚来源不符合条件")
    return record


def _before_body(transactions: SQLiteWorkspaceTransactionStore, digest: str | None) -> bytes:
    """普通文件的原before必须有合法Blob身份，缺失证明不猜测正文。"""
    if digest is None:
        raise KernelError("delivery_record_invalid", "回滚原镜像证明缺失")
    return transactions.blob(digest)


def resolve_workspace_rollback(original: WorkspaceTransactionPlan) -> ResolvedAction:
    """仅从已校验镜像版本构造逆向资源，不读取或限制原Blob正文的文本编码。"""

    resources = []
    workspace: dict[tuple[str, str], WorkspaceResourceRequest] = {}
    for mutation in original.mutations:
        before, after = mutation.before, mutation.after
        operation = (
            "delete"
            if before.presence == "absent"
            else "create"
            if after.presence == "absent"
            else "replace"
        )
        resources.append(
            canonical_action_resource(
                kind="workspace",
                access="write",
                identifier={"location": "workspace", "path": mutation.path},
                attributes={
                    "operation": operation,
                    "expected_sha256": after.sha256,
                    "after_sha256": before.sha256,
                    "mode": before.mode,
                },
            )
        )
        workspace[(mutation.path, "write")] = WorkspaceResourceRequest(
            path=mutation.path, access="write"
        )
        parents = mutation.path.split("/")[:-1]
        for index in range(len(parents) + 1):
            parent = "/".join(parents[:index]) or "."
            workspace[(parent, "read")] = WorkspaceResourceRequest(path=parent, access="read")
    return ResolvedAction(
        resources=tuple(resources),
        workspace_resources=tuple(workspace[key] for key in sorted(workspace)),
    )


class WorkspaceRollbackTransactionPlanner:
    """查询优先地保存同身份逆向计划，严格保留第三内容与原事务。"""

    def __init__(
        self, transactions: SQLiteWorkspaceTransactionStore, workspace_root: WorkspaceRootResolver
    ) -> None:
        self.transactions = transactions
        self._workspace_root = workspace_root

    def prepare(self, route: ActionRoutePlan, arguments: BaseModel) -> WorkspaceTransactionRecord:
        """只在计划尚不存在时读取原镜像并规划；复用路径不重写文件。"""

        original = self._validate_route(route, arguments)
        try:
            record = self.transactions.load(route.execution.plan_id)
        except KernelError as error:
            if error.code != "delivery_transaction_not_found":
                raise
        else:
            self._validate_record(route, original.plan, record.plan)
            return record
        root = self._workspace_root(route.execution.workspace.workspace_id)
        desired = {
            mutation.path: DesiredWorkspaceFile(
                None
                if mutation.before.presence == "absent"
                else _before_body(self.transactions, mutation.before.sha256),
                mutation.before.mode,
            )
            for mutation in original.plan.mutations
        }
        try:
            prepared = prepare_workspace_transaction(
                root,
                desired,
                request_id=f"action:{route.execution.plan_id}",
                transaction_id=route.execution.plan_id,
                platform=route.execution.workspace.platform,
            )
        except KernelError as error:
            # 原Mutation保证before/after不同；当前已是回滚目标也属于前置版本冲突。
            if error.code == "delivery_no_change":
                raise KernelError(
                    "workspace_rollback_conflict", "Patch回滚目标存在后续改动"
                ) from None
            raise
        self._validate_record(route, original.plan, prepared.plan)
        return self.transactions.save(prepared)

    def load(self, route: ActionRoutePlan, arguments: BaseModel) -> WorkspaceTransactionRecord:
        """执行和恢复只加载原计划；缺失记录不能自动重建或补写。"""

        original = self._validate_route(route, arguments)
        record = self.transactions.load(route.execution.plan_id)
        self._validate_record(route, original.plan, record.plan)
        return record

    def _validate_route(
        self, route: ActionRoutePlan, arguments: BaseModel
    ) -> WorkspaceTransactionRecord:
        proposal = decode_workspace_rollback_input(arguments.model_dump(mode="json"))
        original = _original(
            self.transactions, proposal.transaction_id, route.execution.workspace.workspace_id
        )
        resources = resolve_workspace_rollback(original.plan).resources
        if (
            route.binding != workspace_rollback_binding()
            or route.invocation.arguments != proposal.model_dump(mode="json")
            or route.execution.plan_id != route.invocation.invocation_id
            or tuple(
                (r.kind, r.access, r.identifier_sha256, r.attributes_sha256)
                for r in route.resources
            )
            != tuple(
                sorted(
                    (r.kind, r.access, r.identifier_sha256, r.attributes_sha256) for r in resources
                )
            )
        ):
            raise KernelError("delivery_action_mismatch", "回滚事务与原Action不一致")
        return original

    @staticmethod
    def _validate_record(
        route: ActionRoutePlan,
        original: WorkspaceTransactionPlan,
        inverse: WorkspaceTransactionPlan,
    ) -> None:
        if (
            inverse.transaction_id != route.execution.plan_id
            or inverse.request_id != f"action:{route.execution.plan_id}"
            or inverse.source != route.execution.workspace
            or len(inverse.mutations) != len(original.mutations)
        ):
            raise KernelError("delivery_action_mismatch", "逆向事务未绑定原Action来源")
        if any(
            (new.path, new.before, new.after) != (old.path, old.after, old.before)
            for old, new in zip(original.mutations, inverse.mutations, strict=True)
        ):
            raise KernelError("workspace_rollback_conflict", "Patch回滚目标存在后续改动")


def build_workspace_rollback_definition(
    transactions: SQLiteWorkspaceTransactionStore,
    leases: WorkspaceLeaseStore,
    workspace_root: WorkspaceRootResolver,
) -> TrustedActionDefinition:
    """复用原事务Executor，不把原事务批准当作回滚授权。"""

    planner = WorkspaceRollbackTransactionPlanner(transactions, workspace_root)

    def resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        proposal = decode_workspace_rollback_input(arguments.model_dump(mode="json"))
        if context.cwd != ".":
            raise KernelError("workspace_patch_cwd_unsupported", "Patch回滚只支持根级cwd")
        snapshot = capture_workspace_snapshot(
            context.workspace_root, platform=context.capabilities.platform
        )
        original = _original(transactions, proposal.transaction_id, snapshot.workspace_id)
        return resolve_workspace_rollback(original.plan)

    return TrustedActionDefinition(
        binding=workspace_rollback_binding(),
        input_model=WorkspaceRollbackInput,
        resolve=resolve,
        executor=WorkspaceTransactionActionExecutor(
            transactions, leases, workspace_root, planner.load
        ),
    )
