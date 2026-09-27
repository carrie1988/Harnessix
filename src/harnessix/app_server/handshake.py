"""初始化的纯候选构造；响应保护通过前不修改连接身份、能力或限额。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError

from harnessix.app_server.frame_publication import encode, error_frame
from harnessix.protocol.contracts import (
    AGENT_PROTOCOL_VERSION,
    InitializeParams,
    InitializeResult,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    ProtocolLimits,
    ServerCapabilities,
    ServerInfo,
    validate_protocol_input,
)


@dataclass(frozen=True, slots=True)
class PreparedInitialization:
    """未提交握手候选；原响应字节通过检查后由Server一次提交，无数据库状态。"""

    frame: bytes
    client_instance_id: UUID
    item_deltas_enabled: bool
    limits: ProtocolLimits


def validation_path(error: ValidationError) -> tuple[str | int, ...]:
    """保持既有有界路径语义；路径本身仍须经完整出站保护。"""
    first = error.errors(include_url=False, include_context=False)[0]
    return tuple(first.get("loc", ()))[:64]


def prepare_initialization(
    request: JsonRpcRequest,
    *,
    is_new: bool,
    limits: ProtocolLimits,
    methods: tuple[str, ...],
    artifact_pages: bool,
    version: str,
) -> bytes | PreparedInitialization:
    """核对原合同和协商限额，返回错误或纯候选；不改变连接或执行领域命令。"""
    if not is_new:
        return error_frame(request.id, -32600, "already_initialized", "连接已经执行初始化")
    try:
        params = validate_protocol_input(InitializeParams, request.params)
    except (ValidationError, TypeError, ValueError) as error:
        return error_frame(
            request.id,
            -32602,
            "invalid_params",
            "initialize参数无效",
            path=validation_path(error) if isinstance(error, ValidationError) else (),
        )
    if params.protocol_version != AGENT_PROTOCOL_VERSION:
        return error_frame(
            request.id,
            -32602,
            "unsupported_protocol_version",
            "不支持的Agent Protocol版本",
            path=("protocolVersion",),
        )
    effective = ProtocolLimits(
        max_message_bytes=min(limits.max_message_bytes, params.limits.max_message_bytes),
        max_pending_requests=min(limits.max_pending_requests, params.limits.max_pending_requests),
        max_outbound_messages=min(
            limits.max_outbound_messages, params.limits.max_outbound_messages
        ),
        max_replay_events=min(limits.max_replay_events, params.limits.max_replay_events),
    )
    result = InitializeResult(
        server_info=ServerInfo(version=version),
        capabilities=ServerCapabilities(
            methods=methods,
            artifact_pages=artifact_pages,
            item_deltas=params.capabilities.item_deltas,
        ),
        limits=effective,
    )
    return PreparedInitialization(
        frame=encode(
            JsonRpcSuccessResponse(
                id=request.id, result=result.model_dump(mode="json", by_alias=True)
            )
        ),
        client_instance_id=params.client_instance_id,
        item_deltas_enabled=params.capabilities.item_deltas,
        limits=effective,
    )
