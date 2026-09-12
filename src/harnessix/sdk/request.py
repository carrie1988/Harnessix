"""Agent SDK出站帧、协商前置门禁与Response关联。"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import JsonValue

from harnessix.protocol.contracts import (
    InitializeResult,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    ProtocolLimits,
    ProtocolModel,
)
from harnessix.sdk.errors import AgentSDKError
from harnessix.sdk.response import _decode_response


class _ExchangeTransport(Protocol):
    async def exchange(self, frame: bytes) -> tuple[bytes, ...]: ...


def _frame(message: ProtocolModel) -> bytes:
    return (
        json.dumps(
            message.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def require_replay_limit(initialized: InitializeResult | None, limit: int) -> None:
    if initialized is not None and limit > initialized.limits.max_replay_events:
        raise AgentSDKError(
            "negotiated_limit_exceeded",
            "请求Replay数量超过协商上限",
        )


async def exchange_agent_request(
    transport: _ExchangeTransport,
    method: str,
    params: dict[str, JsonValue],
    request_id: int,
    initialized: InitializeResult | None,
) -> JsonValue:
    """在Transport写入前执行协商门禁，并严格关联唯一Response。"""

    if (
        method != "initialize"
        and initialized is not None
        and method not in initialized.capabilities.methods
    ):
        raise AgentSDKError("method_not_negotiated", "App Server未协商当前方法")
    frame = _frame(JsonRpcRequest(id=request_id, method=method, params=params))
    message_limit = (
        ProtocolLimits().max_message_bytes
        if initialized is None
        else initialized.limits.max_message_bytes
    )
    if len(frame) > message_limit:
        raise AgentSDKError("negotiated_limit_exceeded", "Request超过协商消息字节上限")
    responses = await transport.exchange(frame)
    if len(responses) != 1:
        raise AgentSDKError("invalid_response", "Request未收到唯一Response")
    response = _decode_response(responses[0], max_message_bytes=message_limit)
    if response.id != request_id:
        raise AgentSDKError("invalid_response", "Response身份不匹配")
    if isinstance(response, JsonRpcErrorResponse):
        error = response.error
        raise AgentSDKError(
            error.data.code,
            error.message,
            retryable=error.data.retryable,
            path=error.data.path,
        )
    return response.result
