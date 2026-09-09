from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from jsonschema import Draft202012Validator, SchemaError
from pydantic import ConfigDict, JsonValue, RootModel

from harnessix.agent.errors import KernelError

MAX_MCP_SCHEMA_BYTES = 128 * 1024
MAX_MCP_SCHEMA_DEPTH = 32
MAX_MCP_SCHEMA_NODES = 2048
MAX_MCP_ARGUMENT_BYTES = 256 * 1024
MAX_MCP_ARGUMENT_DEPTH = 64
MAX_MCP_ARGUMENT_NODES = 10_000
MAX_MCP_OUTPUT_BYTES = 1024 * 1024

_DRAFT_2020_12 = frozenset(
    {
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }
)
_UNBOUNDED_REGEX_KEYWORDS = frozenset({"pattern", "patternProperties"})


class McpToolArguments(RootModel[dict[str, JsonValue]]):
    model_config = ConfigDict(frozen=True, strict=True, allow_inf_nan=False)


def canonical_json_object(value: Mapping[str, object], *, error_code: str) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        decoded = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise KernelError(error_code, "MCP数据不是规范JSON对象") from None
    if type(decoded) is not dict:
        raise KernelError(error_code, "MCP数据必须是JSON对象")
    return cast(dict[str, JsonValue], decoded)


def validate_mcp_input_schema(value: Mapping[str, object]) -> dict[str, JsonValue]:
    schema = canonical_json_object(value, error_code="mcp_tool_schema_invalid")
    encoded = _json_bytes(schema, "mcp_tool_schema_invalid")
    if len(encoded) > MAX_MCP_SCHEMA_BYTES:
        raise KernelError("mcp_tool_schema_invalid", "MCP Tool Schema超过字节上限")
    if schema.get("type") != "object":
        raise KernelError("mcp_tool_schema_invalid", "MCP Tool输入Schema根必须是object")
    dialect = schema.get("$schema")
    if dialect is not None and dialect not in _DRAFT_2020_12:
        raise KernelError("mcp_tool_schema_invalid", "MCP Tool Schema方言不受支持")
    _inspect_json_tree(
        schema,
        max_depth=MAX_MCP_SCHEMA_DEPTH,
        max_nodes=MAX_MCP_SCHEMA_NODES,
        error_code="mcp_tool_schema_invalid",
        inspect_schema=True,
    )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise KernelError(
            "mcp_tool_schema_invalid", "MCP Tool Schema不符合JSON Schema 2020-12"
        ) from None
    return schema


def validate_mcp_arguments(
    schema: Mapping[str, object], arguments: Mapping[str, object]
) -> McpToolArguments:
    checked_schema = validate_mcp_input_schema(schema)
    checked_arguments = canonical_json_object(arguments, error_code="tool_invalid_arguments")
    if len(_json_bytes(checked_arguments, "tool_invalid_arguments")) > MAX_MCP_ARGUMENT_BYTES:
        raise KernelError("tool_invalid_arguments", "MCP Tool参数超过字节上限")
    _inspect_json_tree(
        checked_arguments,
        max_depth=MAX_MCP_ARGUMENT_DEPTH,
        max_nodes=MAX_MCP_ARGUMENT_NODES,
        error_code="tool_invalid_arguments",
        inspect_schema=False,
    )
    try:
        error = next(Draft202012Validator(checked_schema).iter_errors(checked_arguments), None)
    except Exception:
        raise KernelError("tool_invalid_arguments", "MCP Tool参数验证失败") from None
    if error is not None:
        raise KernelError("tool_invalid_arguments", "MCP Tool参数不符合捕获Schema")
    return McpToolArguments(checked_arguments)


def validate_mcp_structured_output(
    schema: Mapping[str, object], value: object
) -> dict[str, JsonValue]:
    checked_schema = validate_mcp_input_schema(schema)
    checked_value = bounded_mcp_output(value)
    if not isinstance(checked_value, dict):
        raise KernelError("mcp_result_schema_invalid", "MCP Tool结构化结果必须是JSON对象")
    try:
        error = next(Draft202012Validator(checked_schema).iter_errors(checked_value), None)
    except Exception:
        raise KernelError("mcp_result_schema_invalid", "MCP Tool结果Schema验证失败") from None
    if error is not None:
        raise KernelError("mcp_result_schema_invalid", "MCP Tool结果不符合捕获Schema")
    return checked_value


def bounded_mcp_output(value: object) -> JsonValue:
    try:
        encoded = _json_bytes(value, "mcp_result_invalid")
        if len(encoded) > MAX_MCP_OUTPUT_BYTES:
            raise KernelError("mcp_result_too_large", "MCP Tool结果超过字节上限")
        decoded = json.loads(encoded)
    except KernelError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise KernelError("mcp_result_invalid", "MCP Tool结果不是规范JSON") from None
    _inspect_json_tree(
        decoded,
        max_depth=MAX_MCP_ARGUMENT_DEPTH,
        max_nodes=MAX_MCP_ARGUMENT_NODES,
        error_code="mcp_result_invalid",
        inspect_schema=False,
    )
    return cast(JsonValue, decoded)


def _json_bytes(value: object, error_code: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise KernelError(error_code, "MCP数据不是规范JSON") from None


def _inspect_json_tree(
    value: object,
    *,
    max_depth: int,
    max_nodes: int,
    error_code: str,
    inspect_schema: bool,
) -> None:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise KernelError(error_code, "MCP JSON结构超过复杂度上限")
        if isinstance(current, dict):
            for key, child in current.items():
                if type(key) is not str or len(key.encode("utf-8")) > 16 * 1024:
                    raise KernelError(error_code, "MCP JSON对象键无效")
                if inspect_schema:
                    if key in _UNBOUNDED_REGEX_KEYWORDS:
                        raise KernelError(error_code, "MCP Tool Schema包含未受支持的正则关键字")
                    if key == "$id":
                        raise KernelError(error_code, "MCP Tool Schema不得改变引用基址")
                    if key == "$ref" and (type(child) is not str or not child.startswith("#")):
                        raise KernelError(error_code, "MCP Tool Schema不得引用外部资源")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str) and len(current.encode("utf-8")) > 64 * 1024:
            raise KernelError(error_code, "MCP JSON字符串超过字节上限")
