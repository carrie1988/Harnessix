"""严格解码不可信Agent Protocol Response并归一Result校验错误。"""

from __future__ import annotations

import json

from pydantic import JsonValue, ValidationError

from harnessix.protocol.contracts import (
    MAX_PROTOCOL_COLLECTION,
    JsonRpcErrorResponse,
    JsonRpcSuccessResponse,
    ProtocolModel,
    validate_server_output,
)
from harnessix.sdk.errors import AgentSDKError

_MAX_RESPONSE_JSON_DEPTH = 64


def _reject_response_constant(value: str) -> None:
    raise ValueError(f"不支持的JSON常量：{value}")


def _reject_response_duplicate_pairs(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON对象包含重复字段")
        result[key] = value
    return result


def _validate_response_json_budget(value: JsonValue, *, depth: int = 1) -> None:
    if depth > _MAX_RESPONSE_JSON_DEPTH:
        raise ValueError("JSON递归深度超过限制")
    if isinstance(value, dict):
        if len(value) > MAX_PROTOCOL_COLLECTION:
            raise ValueError("JSON对象字段数量超过限制")
        for key, child in value.items():
            if len(key) > MAX_PROTOCOL_COLLECTION:
                raise ValueError("JSON对象字段名超过限制")
            _validate_response_json_budget(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_PROTOCOL_COLLECTION:
            raise ValueError("JSON数组长度超过限制")
        for child in value:
            _validate_response_json_budget(child, depth=depth + 1)


def _decode_response(
    frame: bytes,
    *,
    max_message_bytes: int,
) -> JsonRpcSuccessResponse | JsonRpcErrorResponse:
    """在读取Result前严格验证不可信服务端Response Envelope。"""

    if not frame or len(frame) > max_message_bytes:
        raise AgentSDKError("invalid_response", "App Server Response长度无效")
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError:
        raise AgentSDKError("invalid_response", "App Server Response不是有效UTF-8") from None
    if "\n" in text.rstrip("\r\n") or not text.strip():
        raise AgentSDKError("invalid_response", "App Server Response不是单个JSON对象")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_response_duplicate_pairs,
            parse_constant=_reject_response_constant,
        )
        _validate_response_json_budget(value)
        if not isinstance(value, dict):
            raise ValueError("Response不是JSON对象")
        has_result = "result" in value
        has_error = "error" in value
        if has_result == has_error:
            raise ValueError("Response必须且只能包含result或error")
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if has_result:
            return JsonRpcSuccessResponse.model_validate_json(encoded)
        return JsonRpcErrorResponse.model_validate_json(encoded)
    except (json.JSONDecodeError, RecursionError, ValidationError, ValueError):
        raise AgentSDKError("invalid_response", "App Server Response不符合协议合同") from None


def _result_validation_path(error: ValidationError) -> tuple[str | int, ...]:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if not details:
        return ()
    return tuple(
        part
        for part in details[0].get("loc", ())
        if isinstance(part, str) or isinstance(part, int) and not isinstance(part, bool)
    )


def _validate_result[ProtocolOutput: ProtocolModel](
    model: type[ProtocolOutput], value: object
) -> ProtocolOutput:
    """保留向前兼容字段，并把已知Result合同失败归一为SDK错误。"""

    try:
        return validate_server_output(model, value)
    except ValidationError as error:
        raise AgentSDKError(
            "invalid_result",
            "App Server Result不符合协议合同",
            path=_result_validation_path(error),
        ) from None
    except (TypeError, ValueError):
        raise AgentSDKError("invalid_result", "App Server Result无法按协议解析") from None
