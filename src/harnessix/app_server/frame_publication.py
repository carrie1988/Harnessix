"""原JSON-RPC封套准入和出站字节保护；固定控制错误不回显未检查字段。"""

from __future__ import annotations

import json

from harnessix.agent.errors import KernelError
from harnessix.agent.input_publication import INPUT_FAILURE_CODES
from harnessix.agent.runtime import AgentRuntime
from harnessix.protocol.codec import ProtocolDecodeError, decode_client_frame
from harnessix.protocol.contracts import (
    JsonRpcError,
    JsonRpcErrorData,
    JsonRpcErrorResponse,
    JsonRpcId,
    JsonRpcNotification,
    JsonRpcRequest,
    ProtocolModel,
)

OUTPUT_FAILURE_CODES = frozenset(
    {
        "public_output_secret_leak",
        "public_output_secret_unavailable",
        "public_output_limit",
        "public_output_timeout",
        "public_output_protection_failed",
    }
)


def encode(message: ProtocolModel) -> bytes:
    """按既有UTF8与单行合同编码原DTO，不替换身份、字段或正文。"""
    return (
        json.dumps(
            message.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def error_frame(
    request_id: JsonRpcId | None,
    rpc_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    path: tuple[str | int, ...] = (),
) -> bytes:
    """构造既有错误封套；动态错误必须通过出站保护，控制失败使用固定字段。"""
    return encode(
        JsonRpcErrorResponse(
            id=request_id,
            error=JsonRpcError(
                code=rpc_code,
                message=message,
                data=JsonRpcErrorData(code=code, retryable=retryable, path=path),
            ),
        )
    )


async def admit_message(
    runtime: AgentRuntime, message: JsonRpcRequest | JsonRpcNotification
) -> tuple[bytes, ...] | None:
    """先检查相关ID，再检查完整原封套；拒绝通知不响应，未授权ID使用null。"""
    checked_id: JsonRpcId | None = None
    try:
        if isinstance(message, JsonRpcRequest):
            await runtime.validate_public_input({"id": message.id})
            checked_id = message.id
        await runtime.validate_public_input(message.model_dump(mode="json", by_alias=True))
    except KernelError as error:
        if isinstance(message, JsonRpcNotification):
            return ()
        code = (
            error.code
            if error.code in INPUT_FAILURE_CODES.values()
            else "public_input_protection_failed"
        )
        return (error_frame(checked_id, -32010, code, "协议输入未通过公开数据保护校验"),)
    return None


async def publish_frame(
    runtime: AgentRuntime, frame: bytes, checked_id: JsonRpcId, *, max_message_bytes: int
) -> tuple[bytes, bool]:
    """原响应字节检查先于Writer；失败保留已授权相关ID并仅发固定控制错误。"""
    if len(frame) > max_message_bytes:
        return error_frame(
            checked_id,
            -32010,
            "response_too_large",
            "响应超过协商字节上限，请减少分页条数或读取Artifact分页",
        ), False
    try:
        await runtime.validate_public_frame(frame)
    except KernelError as error:
        code = (
            error.code if error.code in OUTPUT_FAILURE_CODES else "public_output_protection_failed"
        )
        return error_frame(checked_id, -32010, code, "协议输出未通过公开数据保护校验"), False
    return frame, True


def closing_response(frame: bytes, *, max_message_bytes: int) -> tuple[bytes, ...]:
    """关闭后只用有界Codec区分通知；不准入业务，不回应合法通知，不回显原身份。"""
    try:
        message = decode_client_frame(frame, max_message_bytes=max_message_bytes)
        if isinstance(message, JsonRpcNotification):
            return ()
    except ProtocolDecodeError:
        pass
    return (error_frame(None, -32015, "server_closing", "服务端正在关闭"),)
