"""从注册输入合同生成有界字段反馈，不回显调用值或校验异常正文。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import JsonValue

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
_COMPOSED_KEYS = frozenset(
    {"$ref", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentRequired"}
)
_INSTRUCTION = "请按已公布的input_schema修正类型、范围及必填字段。"
_MAX_TYPE_MISMATCHES = 8

_JsonType = Literal["string", "array", "object", "boolean", "integer", "number", "null"]
_JSON_TYPE_ORDER: tuple[_JsonType, ...] = (
    "string",
    "array",
    "object",
    "boolean",
    "integer",
    "number",
    "null",
)
_JSON_TYPES = frozenset(_JSON_TYPE_ORDER)


def _field_facts(
    schema: Mapping[str, JsonValue],
) -> tuple[list[str], list[str], dict[str, JsonValue]] | None:
    """只投影直接object合同；条件、引用或不完整元数据不猜测字段。"""
    if schema.get("type") != "object" or any(key in schema for key in _COMPOSED_KEYS):
        return None
    properties, required = schema.get("properties"), schema.get("required", [])
    if type(properties) is not dict or type(required) is not list:
        return None
    if len(properties) > 64 or len(required) > 64:
        return None
    if any(type(name) is not str or _FIELD_NAME.fullmatch(name) is None for name in properties):
        return None
    if any(type(name) is not str or name not in properties for name in required):
        return None
    if len(set(required)) != len(required):
        return None
    return (
        sorted(properties),
        sorted(cast(list[str], required)),
        properties,
    )


def _normalized_types(value: JsonValue) -> tuple[_JsonType, ...] | None:
    if type(value) is str:
        return (value,) if value in _JSON_TYPES else None
    if type(value) is not list or not value or len(value) > len(_JSON_TYPE_ORDER):
        return None
    if any(type(item) is not str or item not in _JSON_TYPES for item in value):
        return None
    if len(set(cast(list[str], value))) != len(value):
        return None
    return tuple(json_type for json_type in _JSON_TYPE_ORDER if json_type in value)


def _expected_json_types(value: JsonValue) -> tuple[_JsonType, ...] | None:
    """只读取直接type或由纯type分支组成的一层oneOf/anyOf。"""
    if type(value) is not dict:
        return None
    schema = value
    compositions = [key for key in ("oneOf", "anyOf") if key in schema]
    if "type" in schema:
        if any(key in schema for key in _COMPOSED_KEYS):
            return None
        return _normalized_types(schema["type"])
    if len(compositions) != 1 or any(key in schema for key in _COMPOSED_KEYS - {"oneOf", "anyOf"}):
        return None
    options = schema[compositions[0]]
    if type(options) is not list or not options or len(options) > len(_JSON_TYPE_ORDER):
        return None
    merged: set[_JsonType] = set()
    for option in options:
        if type(option) is not dict or any(key in option for key in _COMPOSED_KEYS):
            return None
        option_schema = option
        if "type" not in option_schema:
            return None
        option_types = _normalized_types(option_schema["type"])
        if option_types is None:
            return None
        merged.update(option_types)
    return tuple(json_type for json_type in _JSON_TYPE_ORDER if json_type in merged)


def _actual_json_type(value: JsonValue) -> _JsonType | None:
    if value is None:
        return "null"
    value_type = type(value)
    if value_type is bool:
        return "boolean"
    if value_type is int:
        return "integer"
    if value_type is float:
        return "number"
    if value_type is str:
        return "string"
    if value_type is list:
        return "array"
    if value_type is dict:
        return "object"
    return None


def _type_mismatches(
    properties: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]
) -> list[str]:
    mismatches: list[str] = []
    for name in sorted(properties):
        if name not in arguments:
            continue
        expected = _expected_json_types(properties[name])
        actual = _actual_json_type(arguments[name])
        if expected is None or actual is None:
            continue
        compatible = actual in expected or (actual == "integer" and "number" in expected)
        if compatible:
            continue
        mismatches.append(f"{name} 应为 {' 或 '.join(expected)}，实际为 {actual}")
        if len(mismatches) == _MAX_TYPE_MISMATCHES:
            break
    return mismatches


def invalid_argument_message(
    schema: Mapping[str, JsonValue],
    arguments: Mapping[str, JsonValue],
    *,
    trusted_action: bool = False,
) -> str:
    """只公开正式字段、缺失事实和JSON类型；参数值及异常不能进入消息。"""
    label = "Action参数不符合Trusted Tool契约" if trusted_action else "工具参数不符合契约"
    fallback = f"{label}；{_INSTRUCTION}"
    fields = _field_facts(schema)
    if fields is None:
        return fallback
    allowed, required, properties = fields
    missing = [name for name in required if name not in arguments]
    instruction = (
        _INSTRUCTION[:-1] + "，禁止额外字段。"
        if schema.get("additionalProperties") is False
        else _INSTRUCTION
    )
    field_feedback = (
        f"{label}；必填字段：{', '.join(required) or '无'}；"
        f"缺少必填字段：{', '.join(missing) or '无'}；允许字段：{', '.join(allowed) or '无'}。"
    )
    previous_message = f"{field_feedback}{instruction}"
    mismatches = _type_mismatches(properties, arguments)
    message = (
        f"{field_feedback}类型不匹配：{'；'.join(mismatches)}。{instruction}"
        if mismatches
        else previous_message
    )
    if len(message) <= 2000:
        return message
    return previous_message if len(previous_message) <= 2000 else fallback
