"""Agent预规划测试装配：原SQLite Router/Gateway及耐久Workspace CAS。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolCallContent, Turn
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ToolDescriptor
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.agent_gateway_invocation import build_agent_action_invocation
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    ActionRouteSnapshot,
    CodingActionInvocation,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2
from tests.trusted_actions.test_agent_gateway import agent_state, descriptor, runtime_context


class PreparedFileInput(BaseModel):
    """默认值用于证明参数规范化先于Provider，禁止模型携带计划控制字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    path: str = Field(default="file.txt", min_length=1, max_length=256)


class UnrenderablePreparationError(KernelError):
    """异常转换不得尝试渲染Provider持有的内部正文。"""

    def __str__(self) -> str:
        raise AssertionError("不得渲染原准备异常")

    def __repr__(self) -> str:
        raise AssertionError("不得渲染原准备异常")


def _prepared_tool() -> tuple[ToolDescriptor, TrustedToolBinding]:
    tool = descriptor().model_copy(update={"input_schema": PreparedFileInput.model_json_schema()})
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=tool.name,
        tool_version=tool.version,
        tool_fingerprint=tool_fingerprint(tool),
        input_schema_sha256=canonical_digest(tool.input_schema),
        effect_class=tool.effect_class,
        risk_level=tool.risk_level,
        recovery_mode="durable_ledger",
        executor_id="test.preplanning",
    )
    return tool, binding


def file_resolution(path: str = "file.txt") -> ResolvedAction:
    return ResolvedAction(
        resources=(
            canonical_action_resource(
                kind="workspace",
                access="write",
                identifier={"location": "workspace", "path": path},
            ),
        ),
        workspace_resources=(WorkspaceResourceRequest(path=path, access="write"),),
    )


@dataclass
class WriteSentinel:
    """仅Executor调用才落盘的哨兵；准备与Review不能触发此效果。"""

    marker: Path
    calls: int = 0
    reconciliations: int = 0

    async def execute(self, _plan: object, _arguments: BaseModel) -> ActionExecutionOutcome:
        self.calls += 1
        self.marker.write_text("executed", encoding="utf-8")
        return ActionExecutionOutcome(kind="succeeded")

    async def reconcile(self, _plan: object, _arguments: BaseModel) -> ActionExecutionOutcome:
        self.reconciliations += 1
        self.marker.write_text("reconciled", encoding="utf-8")
        return ActionExecutionOutcome(kind="succeeded")


class RecordingPolicy(DefaultCodingRiskPolicy):
    """记录时序并实际调用原风险策略，不制造审批结论。"""

    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def evaluate(self, *args: Any, **kwargs: Any):
        self.trace.append("policy")
        return super().evaluate(*args, **kwargs)


type PrepareOperation = Callable[
    [
        CodingActionInvocation,
        BaseModel,
        ActionPlanningContext,
        Thread,
        Turn,
        ToolCallContent,
        CancelToken,
    ],
    Awaitable[object],
]


class RecordingPreparer:
    """测试Provider只有资源准备能力；保存入参以核对宿主控制的身份和上下文。"""

    def __init__(self, operation: PrepareOperation | None = None) -> None:
        self.operation = operation
        self.trace: list[str] = []
        self.observed: list[tuple[Any, ...]] = []
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.calls = 0
        self.settled = 0

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
        self.calls += 1
        self.trace.append("prepare")
        self.observed.append((invocation, arguments, context, thread, turn, call, cancel))
        self.entered.set()
        try:
            assert context.checkpoint is not None
            context.checkpoint()
            cancel.checkpoint()
            if self.operation is not None:
                return await self.operation(
                    invocation, arguments, context, thread, turn, call, cancel
                )  # type: ignore[return-value]
            return file_resolution(PreparedFileInput.model_validate(arguments).path)
        finally:
            self.settled += 1
            self.finished.set()


@dataclass
class StoredReview:
    """Review必须观察已落盘的同一Route与Execution，仍不赋予执行批准。"""

    router: TrustedActionRouter
    plans: SQLiteExecutionPlanStore
    trace: list[str]
    calls: int = 0

    async def review(
        self,
        route: ActionRouteSnapshot,
        _thread: Thread,
        _turn: Turn,
        _call: ToolCallContent,
        cancel: CancelToken,
    ) -> TrustedActionReview:
        cancel.checkpoint()
        assert self.router.status(route.plan.execution.plan_id) == route
        assert self.plans.load_plan(route.plan.execution.plan_id) == route.plan.execution
        self.trace.append("review")
        self.calls += 1
        return TrustedActionReview()


@dataclass
class PreplanningHarness:
    root: Path
    state: Path
    context: ActionPlanningContext
    gateway: RouterBackedAgentActionGateway
    router: TrustedActionRouter
    plans: SQLiteExecutionPlanStore
    audit: SQLiteActionAuditStore
    cas: SQLiteWorkspaceTransactionStore
    binding: TrustedToolBinding
    executor: WriteSentinel
    review: StoredReview
    trace: list[str]
    thread: Thread
    turn: Turn
    call: ToolCallContent

    def invocation(self, call: ToolCallContent | None = None) -> CodingActionInvocation:
        return build_agent_action_invocation(
            self.thread, self.turn, call or self.call, self.binding, requires_idempotency=True
        )

    def snapshot(self, resources: tuple[WorkspaceResourceRequest, ...]) -> WorkspaceSnapshotV2:
        ports = WorkspaceSnapshotPorts(self.cas.put_blob, self.cas.blob)
        return capture_workspace_snapshot_v2(
            self.root,
            resources=resources,
            checkpoint=lambda: None,
            platform=self.context.capabilities.platform,
            write_blob=ports.write_blob,
            read_blob=ports.read_blob,
        )

    def assert_no_effect(self) -> None:
        assert self.executor.calls == self.executor.reconciliations == 0
        assert not self.executor.marker.exists()
        assert self.plans._db.execute("SELECT COUNT(*) FROM execution_approvals").fetchone() == (0,)
        assert self.cas._db.execute("SELECT COUNT(*) FROM workspace_transactions").fetchone() == (
            0,
        )

    def assert_unplanned(self) -> None:
        for store, tables in (
            (self.plans, ("execution_plans", "execution_approvals")),
            (self.audit, ("action_route_plans", "action_route_snapshots", "action_audit_events")),
        ):
            for table in tables:
                assert store._db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
            assert not store._db.in_transaction
        assert self.review.calls == 0
        self.assert_no_effect()


def invalid_resolution(harness: PreplanningHarness, shape: str) -> Any:
    """制造原类型的缺字段、额外字段及伪造代际，不能在fixture中自动修复。"""
    result = file_resolution()
    resource, request = result.resources[0], result.workspace_resources[0]
    if shape == "none":
        return None
    if shape == "dict":
        return {"resources": result.resources, "workspace_resources": result.workspace_resources}
    if shape == "duck_with_context":
        return SimpleNamespace(
            resources=result.resources,
            workspace_resources=result.workspace_resources,
            expected_workspace=None,
            context=harness.context,
            policy="allow",
            executor="override",
        )
    if shape == "subclass":

        class DerivedResolved(ResolvedAction):
            pass

        return DerivedResolved(result.resources, result.workspace_resources)
    if shape == "resources_list":
        return replace(result, resources=list(result.resources))
    if shape.startswith("resource_"):
        from harnessix.trusted_actions.contracts import CanonicalActionResource

        if shape == "resource_dict":
            resource = resource.model_dump()
        elif shape == "resource_subclass":

            class DerivedResource(CanonicalActionResource):
                pass

            resource = DerivedResource.model_validate(resource.model_dump())
        elif shape == "resource_incomplete":
            resource = CanonicalActionResource.model_construct(kind="workspace", access="write")
        elif shape == "resource_extra":
            resource = resource.model_copy(deep=True)
            vars(resource)["policy"] = "allow"
        elif shape == "resource_invalid":
            resource = resource.model_copy(update={"access": "connect"})
        elif shape == "resource_wrong_primitive":
            resource = resource.model_copy(update={"identifier_sha256": 7})
        else:
            raise AssertionError(shape)
        return replace(result, resources=(resource,))
    if shape == "workspace_list":
        return replace(result, workspace_resources=list(result.workspace_resources))
    if shape.startswith("workspace_"):
        if shape == "workspace_dict":
            request = request.model_dump()
        elif shape == "workspace_subclass":

            class DerivedRequest(WorkspaceResourceRequest):
                pass

            request = DerivedRequest.model_validate(request.model_dump())
        elif shape == "workspace_extra":
            request = request.model_copy(deep=True)
            vars(request)["extra"] = "unbound"
        elif shape == "workspace_invalid":
            request = request.model_copy(update={"path": 3})
        elif shape == "workspace_invalid_access":
            request = request.model_copy(update={"access": "connect"})
        else:
            raise AssertionError(shape)
        return replace(result, workspace_resources=(request,))
    if shape == "legacy_expected":
        expected = capture_workspace_snapshot(harness.root, resources=result.workspace_resources)
    else:
        expected = harness.snapshot(result.workspace_resources)
        if shape == "expected_subclass":

            class DerivedSnapshot(WorkspaceSnapshotV2):
                pass

            expected = DerivedSnapshot.model_validate(expected.model_dump())
        elif shape == "expected_extra":
            expected = expected.model_copy(deep=True)
            vars(expected)["owner"] = "unbound"
        elif shape == "expected_incomplete":
            expected = expected.model_copy(deep=True)
            del vars(expected)["parent_closure"]
        elif shape == "expected_invalid_revision":
            expected = expected.model_copy(update={"revision": "0" * 64})
        elif shape == "expected_nested_extra":
            expected = expected.model_copy(deep=True)
            vars(expected.parent_closure)["owner"] = "unbound"
        elif shape == "expected_nested_wrong_primitive":
            expected = expected.model_copy(deep=True)
            vars(expected.parent_closure)["parent_count"] = True
        else:
            raise AssertionError(shape)
    return replace(result, expected_workspace=expected)


@contextmanager
def preplanning_harness(
    tmp_path: Path,
    preparer: RecordingPreparer | None = None,
    *,
    use_v2: bool = True,
    checkpoint: Callable[[], None] | None = None,
    resolve: Callable[[BaseModel, ActionPlanningContext], ResolvedAction] | None = None,
    decode_arguments: Callable[..., BaseModel] | None = None,
    state: tuple[Thread, Turn, ToolCallContent] | None = None,
) -> Iterator[PreplanningHarness]:
    """每次重新打开原Stores；关闭连接后可以复验查询优先和跨Store恢复窗口。"""

    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    target = root / "file.txt"
    if not target.exists():
        target.write_text("before", encoding="utf-8")
    store_root = tmp_path / "state"
    trace: list[str] = []
    tool, binding = _prepared_tool()

    def legacy_resolve(arguments: BaseModel, context: ActionPlanningContext) -> ResolvedAction:
        trace.append("resolve")
        if resolve is not None:
            return resolve(arguments, context)
        return file_resolution(PreparedFileInput.model_validate(arguments).path)

    executor = WriteSentinel(tmp_path / "executor-effect.txt")
    with SQLiteWorkspaceTransactionStore(store_root / "workspace-cas") as cas:
        ports = WorkspaceSnapshotPorts(cas.put_blob, cas.blob)
        context = replace(
            runtime_context(root), snapshot_ports=ports if use_v2 else None, checkpoint=checkpoint
        )
        with (
            SQLiteExecutionPlanStore(store_root / "plans.db", read_blob=ports.read_blob) as plans,
            SQLiteActionAuditStore(store_root / "audit.db", read_blob=ports.read_blob) as audit,
        ):
            router = TrustedActionRouter(
                plans=plans,
                audit=audit,
                workspace_root=lambda _: root,
                policy=RecordingPolicy(trace),
                snapshot_ports=ports if use_v2 else None,
            )
            options = {} if preparer is None else {"agent_prepare": preparer}
            if decode_arguments is not None:
                options.update(input_schema=tool.input_schema, decode_arguments=decode_arguments)
            router.register(
                TrustedActionDefinition(
                    binding, PreparedFileInput, legacy_resolve, executor, **options
                )
            )
            review = StoredReview(router, plans, trace)
            gateway = RouterBackedAgentActionGateway(
                router, (tool,), lambda *_: context, reviews=review
            )
            thread, turn, call = state or agent_state(root)
            if state is None:
                call = call.model_copy(update={"tool_fingerprint": tool_fingerprint(tool)})
            harness = PreplanningHarness(
                root,
                store_root,
                context,
                gateway,
                router,
                plans,
                audit,
                cas,
                binding,
                executor,
                review,
                trace,
                thread,
                turn,
                call,
            )
            if preparer is not None:
                preparer.trace = trace
            audit._db.set_trace_callback(
                lambda sql: (
                    trace.append("route")
                    if sql.startswith("INSERT INTO action_route_plans ")
                    else None
                )
            )
            plans._db.set_trace_callback(
                lambda sql: (
                    trace.append("execution")
                    if sql.startswith("INSERT INTO execution_plans ")
                    else None
                )
            )
            try:
                yield harness
            finally:
                gateway.close()
                harness.assert_no_effect()
