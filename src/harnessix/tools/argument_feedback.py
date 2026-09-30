"""从注册输入合同生成有界字段反馈，不读取调用值或校验异常正文。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
_COMPOSED_KEYS = frozenset(
    {"$ref", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentRequired"}
)
_INSTRUCTION = "请按已公布的input_schema修正类型、范围及必填字段。"


def _field_facts(schema: Mapping[str, JsonValue]) -> tuple[list[str], list[str]] | None:
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
    return sorted(properties), sorted(cast(list[str], required))


def invalid_argument_message(
    schema: Mapping[str, JsonValue],
    arguments: Mapping[str, JsonValue],
    *,
    trusted_action: bool = False,
) -> str:
    """只公开正式字段和缺失事实；额外键、参数值及异常都不能进入消息。"""
    label = "Action参数不符合Trusted Tool契约" if trusted_action else "工具参数不符合契约"
    fallback = f"{label}；{_INSTRUCTION}"
    fields = _field_facts(schema)
    if fields is None:
        return fallback
    allowed, required = fields
    missing = [name for name in required if name not in arguments]
    instruction = (
        _INSTRUCTION[:-1] + "，禁止额外字段。"
        if schema.get("additionalProperties") is False
        else _INSTRUCTION
    )
    message = (
        f"{label}；必填字段：{', '.join(required) or '无'}；"
        f"缺少必填字段：{', '.join(missing) or '无'}；允许字段：{', '.join(allowed) or '无'}。"
        f"{instruction}"
    )
    return message if len(message) <= 2000 else fallback
