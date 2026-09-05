"""将受信宿主进程接入现有Action Plane；不注册模型Shell。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import (
    ActionFailure,
    ActionSnapshot,
    EffectClass,
    EffectReceipt,
    ExecutionOutcome,
    ExecutionOutcomeKind,
    ReconciliationOutcome,
    RiskLevel,
)
from harnessix.domain.registry import ToolDefinition
from harnessix.processes.bridge_contracts import PROCESS_ACTION_POLICY
from harnessix.processes.contracts import ProcessRequest, ProcessResult
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.tools.workspace import digest


class ProcessActionInput(ProcessRequest):
    """Action JSON边界接受数组表示tuple，但仍按原严格ProcessRequest验证。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def strict_json_contract(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        try:
            validated = ProcessRequest.model_validate_json(
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError):
            raise ValueError("进程Action输入不符合严格JSON契约") from None
        return validated.model_dump()


class ProcessActionExecutor:
    """每次执行取得独立运行层；Effect Journal拥有审批、租约和未知效果事实。"""

    def __init__(self, factory: Callable[[], HostProcessRuntime]) -> None:
        self._factory = factory
        sample = factory()
        self._binding_fingerprint = sample.binding_fingerprint

    @property
    def version(self) -> str:
        return f"{PROCESS_ACTION_POLICY}.{self._binding_fingerprint}"

    async def execute(self, action: ActionSnapshot, arguments: BaseModel) -> ExecutionOutcome:
        request = ProcessRequest.model_validate_json(arguments.model_dump_json())
        if (
            action.request.tool != action.tool.name
            or action.tool.version != self.version
            or action.tool.effect_class is not EffectClass.NON_IDEMPOTENT_WRITE
            or action.tool.risk_level is not RiskLevel.HIGH
            or not action.tool.requires_idempotency
            or not action.tool.requires_approval
            or action.tool.supports_reconciliation
        ):
            return ExecutionOutcome.failed(
                code="process_tool_contract_changed",
                message="持久命令工具与当前宿主绑定不一致；未启动进程",
            )
        if action.request.secret_refs:
            return ExecutionOutcome.failed(
                code="process_secret_refs_unsupported",
                message="宿主进程尚未配置凭据引用解析；未启动进程",
            )
        try:
            runtime = self._factory()
            if runtime.binding_fingerprint != self._binding_fingerprint:
                return ExecutionOutcome.failed(
                    code="process_binding_changed",
                    message="进程宿主绑定与审批时工具版本不一致；未启动进程",
                )
            async with runtime:
                result = await runtime.run(request, CancelToken())
        except KernelError as error:
            return ExecutionOutcome.failed(
                code=error.code,
                message=error.message,
                retriable=error.retryable,
            )
        receipt = self._receipt(action, result)
        output = result.model_dump(mode="json")
        if result.termination == "failed" or not (result.stdout.eof and result.stderr.eof):
            return ExecutionOutcome(
                kind=ExecutionOutcomeKind.UNKNOWN,
                output=output,
                error=ActionFailure(
                    code="process_effect_unknown",
                    message="进程组清理或输出终止证据不完整；禁止自动重放",
                    retriable=False,
                ),
                receipt=receipt,
            )
        # Action成功表示执行生命周期已确定；命令退出码和停止原因仍由ProcessResult表达。
        return ExecutionOutcome.succeeded(output=output, receipt=receipt)

    async def reconcile(self, action: ActionSnapshot) -> ReconciliationOutcome:
        return ReconciliationOutcome.manual(
            code="process_manual_reconciliation_required",
            message="历史PID不能证明进程或副作用状态；禁止自动终止或重放",
        )

    @staticmethod
    def _receipt(action: ActionSnapshot, result: ProcessResult) -> EffectReceipt:
        return EffectReceipt(
            provider="harnessix.host-process",
            resource_type="process_invocation",
            idempotency_key=action.request.idempotency_key,
            response_digest=digest(result.model_dump(mode="json")),
        )


def process_action_tool(factory: Callable[[], HostProcessRuntime]) -> ToolDefinition:
    """宿主显式注册的高风险动作；默认策略要求审批与幂等键。"""
    executor = ProcessActionExecutor(factory)
    return ToolDefinition(
        name="host.process",
        version=executor.version,
        description="执行宿主预先绑定的程序与argv；非Shell，必须审批，不自动重放",
        input_model=ProcessActionInput,
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        executor=executor,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=False,
    )
