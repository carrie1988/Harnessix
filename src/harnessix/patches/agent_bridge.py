"""受信宿主调用桥接；未注册为 Kernel 工具，不读取或驱动 Session。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
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
from harnessix.patches.bridge_contracts import (
    BRIDGE_POLICY,
    ManagedPatchCallPlan,
    ManagedPatchOutput,
    call_request_id,
)
from harnessix.patches.contracts import PatchProposal
from harnessix.patches.managed import ManagedPatchWorkspace
from harnessix.patches.managed_contracts import PatchRecord
from harnessix.patches.managed_io import fail
from harnessix.patches.planner import prepare_patch
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS, ReadToolError
from harnessix.tools.runtime import _drain
from harnessix.tools.workspace import ReadOperation, digest


@dataclass(frozen=True, slots=True)
class PatchCallResult:
    """result 可提供给模型；plan/record 是宿主证据，不随模型结果序列化。"""

    result: ToolResultContent
    plan: ManagedPatchCallPlan | None
    record: PatchRecord | None


class ManagedPatchBridge:
    def __init__(self, copy: ManagedPatchWorkspace) -> None:
        self._copy = copy
        self._lock = asyncio.Lock()
        self._closed = False
        contract = digest(
            {
                "implementation": BRIDGE_POLICY,
                "workspace_id": str(copy.workspace_id),
                "scope": copy.workspace.scope,
                "input": PatchProposal.model_json_schema(),
                "output": ManagedPatchOutput.model_json_schema(),
                "plan": ManagedPatchCallPlan.model_json_schema(),
                "timeout": READ_TIMEOUT_SECONDS,
            }
        )
        self._definition = ToolDescriptor(
            name="apply_patch",
            version=f"1.{contract}",
            description="仅修改宿主受管副本内一个文件；精确编辑，必须先读取 revision 并批准计划",
            input_schema=PatchProposal.model_json_schema(),
            effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
            risk_level=RiskLevel.HIGH,
            requires_idempotency=True,
            requires_approval=True,
            supports_reconciliation=True,
        )

    def definition(self) -> ToolDescriptor:
        """单一写定义，不实现通用工具运行时，防止误接入旧的只读入口。"""
        return self._definition.model_copy(deep=True)

    def _validate(self, call: ToolCallContent, scope: ToolExecutionScope) -> PatchProposal:
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
            raise KernelError("tool_contract_changed", "Patch 工具或副本契约已变化")
        try:
            return PatchProposal.model_validate_json(json.dumps(call.arguments, allow_nan=False))
        except (ValidationError, ValueError, TypeError):
            raise KernelError("tool_invalid_arguments", "Patch 参数不符合契约") from None

    @staticmethod
    def _request(scope: ToolExecutionScope) -> str:
        return call_request_id(
            scope.thread_id, scope.turn_id, scope.call_id, scope.request_fingerprint
        )

    def _plan(
        self, scope: ToolExecutionScope, proposal: PatchProposal, record: PatchRecord
    ) -> ManagedPatchCallPlan:
        if (
            record.request_id != self._request(scope)
            or record.workspace_id != self._copy.workspace_id
            or record.manifest.workspace_scope != self._copy.workspace.scope
            or record.manifest.proposal_sha256 != digest(proposal.model_dump(mode="json"))
        ):
            raise fail("call_mismatch")
        data = {
            "version": BRIDGE_POLICY,
            "thread_id": str(scope.thread_id),
            "turn_id": str(scope.turn_id),
            "call_id": str(scope.call_id),
            "call_fingerprint": scope.request_fingerprint,
            "request_id": record.request_id,
            "workspace_id": str(record.workspace_id),
            "plan_id": str(record.plan_id),
            "manifest": record.manifest.model_dump(mode="json"),
            "backend_fingerprint": record.approval_fingerprint,
        }
        return ManagedPatchCallPlan.model_validate_json(
            json.dumps({**data, "approval_fingerprint": digest(data)})
        )

    def _load(
        self,
        scope: ToolExecutionScope,
        proposal: PatchProposal,
        plan: ManagedPatchCallPlan,
        operation: ReadOperation,
    ) -> PatchRecord:
        record = self._copy.lookup(self._request(scope), operation)
        if record is None:
            raise fail("plan_not_found")
        # 比较全部字段，也拒绝 model_copy 绕过契约校验后的计划。
        if self._plan(scope, proposal, record) != plan:
            raise fail("call_mismatch")
        return record

    async def _run[T](self, action: Callable[[ReadOperation], T], cancel: CancelToken) -> T:
        cancel.checkpoint()

        async def serial() -> T:
            async with self._lock:
                if self._closed:
                    raise KernelError("tool_runtime_closed", "Patch 桥接已关闭")
                cancel.checkpoint()
                operation = ReadOperation()
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
        self, call: ToolCallContent, scope: ToolExecutionScope, cancel: CancelToken
    ) -> ManagedPatchCallPlan:
        proposal = self._validate(call, scope)

        def prepare(operation: ReadOperation) -> ManagedPatchCallPlan:
            record = self._copy.lookup(self._request(scope), operation)
            if record is None:
                prepared = prepare_patch(self._copy.workspace, proposal, operation)
                record = self._copy.save(prepared, self._request(scope), operation)
            plan = self._plan(scope, proposal, record)
            if record.state != "pending":
                raise fail("not_preparable")
            self._copy.verify(record.plan_id, operation)
            return plan

        return await self._run(prepare, cancel)

    async def review(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        plan: ManagedPatchCallPlan,
        cancel: CancelToken,
    ) -> PatchRecord:
        """写审批答复前复核；不记录答复，不赋予执行许可。"""
        proposal = self._validate(call, scope)

        def review(operation: ReadOperation) -> PatchRecord:
            record = self._load(scope, proposal, plan, operation)
            if record.state != "pending":
                raise fail("approval_closed")
            return self._copy.verify(record.plan_id, operation)

        return await self._run(review, cancel)

    @staticmethod
    def _decision(plan: ManagedPatchCallPlan, approval: ApprovalRecord) -> ApprovalDecision:
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
        plan: ManagedPatchCallPlan,
        approval: ApprovalRecord,
        cancel: CancelToken,
    ) -> PatchCallResult:
        """宿主必须先持久批准并消费恢复边界；本层不验证 Session 活跃性/时限。"""
        proposal = self._validate(call, scope)
        decision = self._decision(plan, approval)

        def execute(operation: ReadOperation) -> PatchCallResult:
            record = self._load(scope, proposal, plan, operation)
            if record.state not in {"pending", "approved"}:
                raise fail("not_executable")
            if decision.outcome == ApprovalOutcome.APPROVED:
                self._copy.verify(record.plan_id, operation)
            operation.checkpoint()
            self._copy.reply(record.plan_id, record.approval_fingerprint, decision)
            operation.checkpoint()
            if decision.outcome == ApprovalOutcome.APPROVED:
                record = self._copy.execute(record.plan_id, record.approval_fingerprint, operation)
            else:
                record = self._copy.get(record.plan_id)
            return self._result(call, plan, record, decision)

        return await self._run(execute, cancel)

    async def recover(
        self,
        call: ToolCallContent,
        scope: ToolExecutionScope,
        cancel: CancelToken,
        *,
        plan: ManagedPatchCallPlan | None = None,
        approval: ApprovalRecord | None = None,
    ) -> PatchCallResult:
        """不准备/批准/执行；只有匹配宿主批准的已归因后镜像才能报告成功。"""
        proposal = self._validate(call, scope)

        def recover(operation: ReadOperation) -> PatchCallResult:
            try:
                record = self._copy.lookup(self._request(scope), operation)
                if record is None:
                    # Session 若已有计划而账本缺失，不能证明效果未发生。
                    return self._failure(
                        call, "unknown" if plan is not None else "failed", "patch_plan_not_found"
                    )
                found = self._plan(scope, proposal, record)
                if plan is not None and found != plan:
                    raise fail("call_mismatch")
                decision = self._decision(found, approval) if approval is not None else None
                record = self._copy.reconcile(record.plan_id, operation)
                return self._result(call, found, record, decision)
            except KernelError as error:
                return self._failure(call, "unknown", error.code)

        return await self._run(recover, cancel)

    @staticmethod
    def _failure(
        call: ToolCallContent, outcome: Literal["unknown", "failed"], code: str
    ) -> PatchCallResult:
        return PatchCallResult(
            ToolResultContent(
                call_id=call.call_id,
                outcome=outcome,
                error=AgentFailure(code=code, message="Patch 未完成或效果尚未确认"),
            ),
            None,
            None,
        )

    @staticmethod
    def _result(
        call: ToolCallContent,
        plan: ManagedPatchCallPlan,
        record: PatchRecord,
        decision: ApprovalDecision | None,
    ) -> PatchCallResult:
        outcome: Literal["unknown", "failed", "succeeded"] = "unknown"
        code = "patch_uncertain_effect"
        if record.state in {"applied", "observed_after"}:
            if decision is not None and decision == record.decision:
                outcome = "succeeded"
            else:
                code = "patch_approval_unverified"
        elif record.state in {"pending", "approved", "rejected", "failed", "observed_before"}:
            outcome = "failed"
            code = record.error_code or (
                "approval_rejected" if record.state == "rejected" else "patch_not_applied"
            )
        result = ToolResultContent(
            call_id=call.call_id,
            outcome=outcome,
            output=ManagedPatchOutput(
                path=record.manifest.path,
                state=record.state,
                before_sha256=record.manifest.before_sha256,
                after_sha256=record.manifest.after_sha256,
            ).model_dump(mode="json"),
            error=None
            if outcome == "succeeded"
            else AgentFailure(code=code, message="Patch 未完成或效果尚未确认"),
        )
        return PatchCallResult(result, plan, record)

    async def aclose(self) -> None:
        self._closed = True

        async def close() -> None:
            async with self._lock:
                pass  # 副本归宿主所有；只等待本桥接活动操作结束。

        closing = asyncio.create_task(close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await _drain(closing)
            raise

    async def __aenter__(self) -> Self:
        if self._closed:
            raise KernelError("tool_runtime_closed", "Patch 桥接已关闭")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
