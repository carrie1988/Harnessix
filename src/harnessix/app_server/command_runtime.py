"""协议命令的持久幂等生命周期；不复制Agent状态机或更改命令身份。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.protocol.contracts import CommandParams, ProtocolModel, validate_protocol_input
from harnessix.protocol.requests import ProtocolRequestError, ProtocolRequestStore


class AgentServiceError(RuntimeError):
    """App Server业务命令返回的稳定服务错误。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def service_error(error: KernelError | ProtocolRequestError) -> AgentServiceError:
    """保留稳定错误码和重试标志；正文须经公开保护后才可存储或返回。"""
    return AgentServiceError(
        error.code, str(error), retryable=isinstance(error, KernelError) and error.retryable
    )


async def execute_command[Params: CommandParams, Result: ProtocolModel](
    runtime: AgentRuntime,
    requests: ProtocolRequestStore,
    client_instance_id: UUID,
    method: str,
    params: Params,
    operation: Callable[[], Awaitable[Result]],
    result_model: type[Result],
    *,
    after_result: Callable[[Result], None] | None = None,
) -> Result:
    """保护原输入后认领命令，重放或执行；原结果检查后结算幂等回执。"""
    wire = params.model_dump(mode="json", by_alias=True)
    try:
        await runtime.validate_public_input(
            {"client_instance_id": str(client_instance_id), "method": method, "params": wire}
        )
    except KernelError as error:
        raise service_error(error) from None
    claimed = False
    try:
        claim = await requests.claim(
            client_instance_id,
            params.request_id,
            method,
            wire,
        )
        claimed = True
        if claim.record.state != "accepted":
            await runtime.validate_public_output(claim.record.outcome)
        if claim.record.state == "completed":
            result = validate_protocol_input(result_model, claim.record.outcome)
            if after_result is not None:
                after_result(result)
            return result
        if claim.record.state == "failed":
            outcome = claim.record.outcome
            if isinstance(outcome, dict):
                raise AgentServiceError(
                    str(outcome.get("code", "command_failed")),
                    str(outcome.get("message", "协议命令失败")),
                    retryable=outcome.get("retryable") is True,
                )
            raise AgentServiceError("command_failed", "协议命令失败")
        result = await operation()
        await runtime.validate_public_output(result.model_dump(mode="json", by_alias=True))
        await requests.complete(
            client_instance_id,
            params.request_id,
            result.model_dump(mode="json", by_alias=True),
        )
        if after_result is not None:
            after_result(result)
        return result
    except (KernelError, ProtocolRequestError) as error:
        failure = service_error(error)
        try:
            await runtime.validate_public_output(
                {
                    "code": failure.code,
                    "message": failure.message,
                    "retryable": failure.retryable,
                }
            )
        except KernelError as protection_error:
            failure = service_error(protection_error)
        if claimed and error.code != "idempotency_conflict":
            try:
                await requests.fail(
                    client_instance_id,
                    params.request_id,
                    {
                        "code": failure.code,
                        "message": failure.message,
                        "retryable": failure.retryable,
                    },
                )
            except ProtocolRequestError:
                pass
        raise failure from None
