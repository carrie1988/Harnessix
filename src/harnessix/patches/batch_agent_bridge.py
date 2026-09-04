"""受信宿主整组桥接；不驱动 Session，不自动注册模型工具。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Self

from pydantic import ValidationError

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.patches.batch_approval_contracts import ManagedPatchBatchApproval
from harnessix.patches.batch_bridge_contracts import (
    BATCH_BRIDGE_POLICY,
    BatchFileOutput,
    ManagedPatchBatchCallPlan,
    ManagedPatchBatchOutput,
    batch_call_request_id,
)
from harnessix.patches.batch_contracts import PatchBatchProposal
from harnessix.patches.batch_run_contracts import BatchExecutionResult
from harnessix.patches.batches import prepare_patch_batch
from harnessix.patches.diff_document import PreparedBatchDiffDocument, batch_diff_document
from harnessix.patches.diff_document_contracts import BatchDiffDocumentOptions
from harnessix.patches.managed import ManagedPatchWorkspace
from harnessix.patches.managed_batches import ManagedPatchBatches
from harnessix.patches.managed_io import fail
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS, ReadToolError
from harnessix.tools.runtime import _drain
from harnessix.tools.workspace import ReadOperation, digest


@dataclass(frozen=True, slots=True)
class BatchCallResult:
    """仅 result 是公开结果；其余字段是宿主私有证据，不进入模型 wire。"""

    result: ToolResultContent
    plan: ManagedPatchBatchCallPlan | None = field(default=None, repr=False)
    approval: ManagedPatchBatchApproval | None = field(default=None, repr=False)
    execution: BatchExecutionResult | None = field(default=None, repr=False)


class ManagedPatchBatchBridge:
    def __init__(self, copy: ManagedPatchWorkspace) -> None:
        self._copy = copy
        self._groups = ManagedPatchBatches(copy)
        self._lock = asyncio.Lock()
        self._closed = False
        contract = digest(
            {
                "implementation": BATCH_BRIDGE_POLICY,
                "workspace_id": str(copy.workspace_id),
                "scope": copy.workspace.scope,
                "input": PatchBatchProposal.model_json_schema(),
                "output": ManagedPatchBatchOutput.model_json_schema(),
                "plan": ManagedPatchBatchCallPlan.model_json_schema(),
                "timeout": READ_TIMEOUT_SECONDS,
            }
        )
        self._definition = ToolDescriptor(
            name="apply_patch_batch",
            version=f"1.{contract}",
            description="仅在受管副本按顺序精确编辑一组已有文件；整组批准一次消费，失败停止且不回滚",
            input_schema=PatchBatchProposal.model_json_schema(),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            requires_idempotency=True,
            requires_approval=True,
            supports_reconciliation=True,
        )

    def definition(self) -> ToolDescriptor:
        return self._definition.model_copy(deep=True)

    def _validate(self, call: ToolCallContent, scope: ToolExecutionScope) -> PatchBatchProposal:
        scope.validate_call(call)
        if scope.workspace != str(self._copy.workspace.root):
            raise fail("workspace_mismatch")
        definition = self._definition
        if (
            call.tool != definition.name
            or call.tool_version != definition.version
            or call.effect_class != definition.effect_class
            or call.requires_approval != definition.requires_approval
            or call.tool_fingerprint != tool_fingerprint(definition)
        ):
            raise KernelError("tool_contract_changed", "整组 Patch 工具或副本契约已变化")
        try:
            return PatchBatchProposal.model_validate_json(
                json.dumps(call.arguments, allow_nan=False)
            )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("tool_invalid_arguments", "整组 Patch 参数不符合契约") from None

    @staticmethod
    def _request(scope: ToolExecutionScope) -> str:
        return batch_call_request_id(
            scope.thread_id, scope.turn_id, scope.call_id, scope.request_fingerprint
        )

    def _plan(
        self,
        scope: ToolExecutionScope,
        proposal: PatchBatchProposal,
        approval: ManagedPatchBatchApproval,
    ) -> ManagedPatchBatchCallPlan:
        backend = approval.plan
        if (
            backend.request_id != self._request(scope)
            or backend.workspace_id != self._copy.workspace_id
            or backend.manifest.workspace_scope != self._copy.workspace.scope
            or backend.manifest.proposal_sha256 != digest(proposal.model_dump(mode="json"))
        ):
            raise fail("call_mismatch")
        data = {
            "version": BATCH_BRIDGE_POLICY,
            "thread_id": str(scope.thread_id),
            "turn_id": str(scope.turn_id),
            "call_id": str(scope.call_id),
            "call_fingerprint": scope.request_fingerprint,
            "backend": backend.model_dump(mode="json"),
        }
        return ManagedPatchBatchCallPlan.model_validate_json(
            json.dumps({**data, "approval_fingerprint": digest(data)})
        )

    def _load(
        self,
        scope: ToolExecutionScope,
        proposal: PatchBatchProposal,
        plan: ManagedPatchBatchCallPlan,
        operation: ReadOperation,
    ) -> ManagedPatchBatchApproval:
        approval = self._groups.lookup(self._request(scope), operation)
        if approval is None:
            raise fail("plan_not_found")
        if self._plan(scope, proposal, approval) != plan:
            raise fail("call_mismatch")
        return approval

    async def _run[T](self, action: Callable[[ReadOperation], T], cancel: CancelToken) -> T:
        cancel.checkpoint()
        operation = ReadOperation()  # 排队时间计入本次预算；Turn 截止时间仍由宿主提供。

        async def serial() -> T:
            async with self._lock:
                if self._closed:
                    raise KernelError("tool_runtime_closed", "整组 Patch 桥接已关闭")
                cancel.checkpoint()
                operation.checkpoint()
                worker = asyncio.create_task(asyncio.to_thread(action, operation))
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    operation.stopped.set()
                    await _drain(worker)
                    raise

        try:
            return await cancel.run(serial())
        except ReadToolError as error:
            raise fail(error.code) from None

    async def prepare(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
    ) -> ManagedPatchBatchCallPlan:
        proposal = self._validate(call, scope)

        def prepare(operation: ReadOperation) -> ManagedPatchBatchCallPlan:
            approval = self._groups.lookup(self._request(scope), operation)
            if approval is None:
                batch = prepare_patch_batch(self._copy.workspace, proposal, operation)
                approval = self._groups.save(batch, self._request(scope), operation)
            plan = self._plan(scope, proposal, approval)
            if approval.decision is not None:
                raise fail("not_preparable")
            self._groups.verify(approval.plan.batch_id, operation)
            return plan

        return await self._run(prepare, cancel)

    async def review(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchBatchCallPlan,
        cancel: CancelToken,
        *,
        verify_source: bool = True,
    ) -> ManagedPatchBatchApproval:
        proposal = self._validate(call, scope)

        def review(operation: ReadOperation) -> ManagedPatchBatchApproval:
            approval = self._load(scope, proposal, plan, operation)
            if approval.decision is not None:
                raise fail("approval_closed")
            return (
                self._groups.verify(approval.plan.batch_id, operation)
                if verify_source
                else approval
            )

        return await self._run(review, cancel)

    @staticmethod
    def _decision(plan: ManagedPatchBatchCallPlan, approval: ApprovalRecord) -> ApprovalDecision:
        if approval.request_fingerprint != plan.approval_fingerprint:
            raise fail("approval_mismatch")
        try:
            return ApprovalDecision.model_validate_json(
                approval.model_dump_json(include={"outcome", "actor", "reason"})
            )
        except ValidationError:
            raise fail("approval_mismatch") from None

    async def execute(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchBatchCallPlan,
        approval: ApprovalRecord,
        cancel: CancelToken,
    ) -> BatchCallResult:
        """宿主必须先持久决定并消费等待边界；本层不校验 Session 活跃性。"""
        proposal = self._validate(call, scope)
        decision = self._decision(plan, approval)

        def execute(operation: ReadOperation) -> BatchCallResult:
            backend = self._load(scope, proposal, plan, operation)
            batch_id = backend.plan.batch_id
            if self._groups.get_execution(batch_id, operation) is not None:
                raise fail("not_executable")
            operation.checkpoint()
            backend = self._groups.reply(
                batch_id, backend.plan.approval_fingerprint, decision, operation
            )
            operation.checkpoint()
            execution = (
                self._groups.execute(batch_id, backend.plan.approval_fingerprint, operation)
                if decision.outcome == ApprovalOutcome.APPROVED
                else None
            )
            return self._result(call, plan, backend, execution)

        return await self._run(execute, cancel)

    async def recover(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
        *,
        plan: ManagedPatchBatchCallPlan | None = None,
        approval: ApprovalRecord | None = None,
    ) -> BatchCallResult:
        """没有原始完整宿主批准不报告效果已确认；恢复不产生新授权或文件写。"""
        proposal = self._validate(call, scope)

        def recover(operation: ReadOperation) -> BatchCallResult:
            try:
                backend = self._groups.lookup(self._request(scope), operation)
                if backend is None:
                    raise fail("plan_not_found")
                found = self._plan(scope, proposal, backend)
                if plan is None or approval is None:
                    raise fail("approval_unverified")
                if found != plan:
                    raise fail("call_mismatch")
                if self._decision(found, approval) != backend.decision:
                    raise fail("approval_unverified")
                execution = self._groups.reconcile(backend.plan.batch_id, operation)
                return self._result(call, found, backend, execution)
            except KernelError as error:
                return BatchCallResult(
                    ToolResultContent(
                        call_id=call.call_id,
                        outcome="unknown",
                        error=AgentFailure(code=error.code, message="整组 Patch 效果无法确认"),
                    )
                )

        return await self._run(recover, cancel)

    async def diff(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchBatchCallPlan,
        cancel: CancelToken,
        *,
        view: Literal["plan", "effect"] = "plan",
        approval: ApprovalRecord | None = None,
        execution: BatchExecutionResult | None = None,
        options: BatchDiffDocumentOptions | None = None,
    ) -> PreparedBatchDiffDocument:
        """只生成未发布报告；不核对目标、不消费批准，也不替代 Session 发布准入。"""
        proposal = self._validate(call, scope)
        plan = ManagedPatchBatchCallPlan.model_validate_json(plan.model_dump_json())
        if (
            view not in {"plan", "effect"}
            or (view == "plan" and (approval is not None or execution is not None))
            or (view == "effect" and approval is None)
        ):
            raise KernelError("patch_diff_view_invalid", "计划与效果报告的证据参数不一致")
        approval = (
            ApprovalRecord.model_validate_json(approval.model_dump_json()) if approval else None
        )
        execution = (
            BatchExecutionResult.model_validate_json(execution.model_dump_json())
            if execution
            else None
        )

        def render(operation: ReadOperation) -> PreparedBatchDiffDocument:
            with self._copy._guard():
                backend, prepared = self._groups._load(plan.backend.batch_id, operation)
                if self._plan(scope, proposal, backend) != plan:
                    raise fail("call_mismatch")
                output = None
                if view == "effect":
                    assert approval is not None
                    if self._decision(plan, approval) != backend.decision:
                        raise fail("approval_unverified")
                    actual = self._groups.get_execution(plan.backend.batch_id, operation)
                    if actual != execution:
                        raise KernelError("patch_diff_effect_mismatch", "报告运行快照与账本不一致")
                    if actual is not None and actual.run.phase != "finished":
                        raise KernelError(
                            "patch_diff_effect_unsettled", "运行未结算，不能生成历史效果"
                        )
                    result = self._result(call, plan, backend, actual)
                    output = ManagedPatchBatchOutput.model_validate_json(
                        json.dumps(result.result.output, allow_nan=False)
                    )
                document = batch_diff_document(
                    self._copy.workspace, prepared, operation, output=output, options=options
                )
                return PreparedBatchDiffDocument(plan, approval, execution, document)

        return await self._run(render, cancel)

    @staticmethod
    def _result(
        call: ToolCallContent,
        plan: ManagedPatchBatchCallPlan,
        approval: ManagedPatchBatchApproval,
        execution: BatchExecutionResult | None,
    ) -> BatchCallResult:
        # 防止另一组或重排运行事实被投影到当前路径。
        try:
            plan = ManagedPatchBatchCallPlan.model_validate_json(plan.model_dump_json())
            approval = ManagedPatchBatchApproval.model_validate_json(approval.model_dump_json())
            if execution is not None:
                execution = BatchExecutionResult.model_validate_json(execution.model_dump_json())
        except ValidationError:
            raise fail("result_mismatch") from None
        if plan.call_id != call.call_id or plan.backend != approval.plan:
            raise fail("result_mismatch")
        if execution is not None and (
            approval.decision is None
            or approval.decision.outcome != ApprovalOutcome.APPROVED
            or (
                execution.run.batch_id,
                execution.run.workspace_id,
                execution.run.approval_fingerprint,
            )
            != (plan.backend.batch_id, plan.backend.workspace_id, plan.backend.approval_fingerprint)
            or tuple(m.plan_id for m in execution.members)
            != tuple(m.plan_id for m in plan.backend.members)
        ):
            raise fail("result_mismatch")
        manifests = plan.backend.manifest.files
        output = ManagedPatchBatchOutput(
            phase=execution.run.phase if execution else "not_started",
            stop_reason=execution.run.stop_reason if execution else None,
            effect=execution.effect if execution else "not_applied",
            files=tuple(
                BatchFileOutput(
                    path=manifest.path,
                    state=execution.members[index].state if execution else "pending",
                    effect=execution.members[index].effect if execution else "not_applied",
                    before_sha256=manifest.before_sha256,
                    after_sha256=manifest.after_sha256,
                )
                for index, manifest in enumerate(manifests)
            ),
        )
        code = (
            "approval_rejected"
            if approval.decision and approval.decision.outcome == ApprovalOutcome.REJECTED
            else "patch_uncertain_effect"
            if output.effect == "unknown"
            else "patch_partial_effect"
            if output.effect == "partial"
            else "patch_not_applied"
        )
        return BatchCallResult(
            ToolResultContent(
                call_id=call.call_id,
                outcome="succeeded"
                if output.effect == "applied"
                else "unknown"
                if output.effect == "unknown"
                else "failed",
                output=output.model_dump(mode="json"),
                error=None
                if output.effect == "applied"
                else AgentFailure(code=code, message="整组 Patch 未全部完成或效果未知"),
            ),
            plan,
            approval,
            execution,
        )

    async def aclose(self) -> None:
        self._closed = True

        async def close() -> None:
            async with self._lock:
                pass  # 只排空桥接，副本仍由宿主所有。

        closing = asyncio.create_task(close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await _drain(closing)
            raise

    async def __aenter__(self) -> Self:
        if self._closed:
            raise KernelError("tool_runtime_closed", "整组 Patch 桥接已关闭")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
