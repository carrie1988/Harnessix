from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue, ValidationError

from harnessix.protocol.contracts import (
    MAX_PROTOCOL_COLLECTION,
    JsonRpcNotification,
    JsonRpcRequest,
)

DEFAULT_MAX_MESSAGE_BYTES = 1_048_576
MAX_JSON_DEPTH = 64


@dataclass(frozen=True, slots=True)
class ProtocolDecodeError(ValueError):
    """可安全映射为JSON-RPC错误的入站帧失败。"""

    rpc_code: int
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _reject_constant(value: str) -> None:
    raise ValueError(f"不支持的JSON常量：{value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON对象包含重复字段")
        result[key] = value
    return result


def _bounded_json(value: JsonValue, *, depth: int = 1) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("JSON递归深度超过限制")
    if isinstance(value, dict):
        if len(value) > MAX_PROTOCOL_COLLECTION:
            raise ValueError("JSON对象字段数量超过限制")
        for key, child in value.items():
            if len(key) > MAX_PROTOCOL_COLLECTION:
                raise ValueError("JSON对象字段名超过限制")
            _bounded_json(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_PROTOCOL_COLLECTION:
            raise ValueError("JSON数组长度超过限制")
        for child in value:
            _bounded_json(child, depth=depth + 1)


def decode_client_frame(
    frame: bytes,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> JsonRpcRequest | JsonRpcNotification:
    """解码一条UTF-8 JSON帧；v1明确拒绝Batch、重复字段和多行输入。"""

    if not frame or len(frame) > max_message_bytes:
        raise ProtocolDecodeError(-32600, "invalid_request", "协议帧长度无效")
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolDecodeError(-32700, "parse_error", "协议帧不是有效UTF-8") from None
    if "\n" in text.rstrip("\r\n") or not text.strip():
        raise ProtocolDecodeError(-32600, "invalid_request", "一帧只能包含一个JSON对象")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        _bounded_json(value)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise ProtocolDecodeError(-32700, "parse_error", "JSON解析失败") from None
    if not isinstance(value, Mapping):
        raise ProtocolDecodeError(-32600, "invalid_request", "JSON-RPC Batch未开放")
    try:
        if "id" in value:
            return JsonRpcRequest.model_validate(value)
        return JsonRpcNotification.model_validate(value)
    except ValidationError:
        raise ProtocolDecodeError(-32600, "invalid_request", "JSON-RPC Envelope无效") from None
