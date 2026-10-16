"""从注册输入合同生成有界字段反馈，不回显调用值或校验异常正文。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import JsonValue

from harnessix.tools.contracts import READ_ARGUMENT_CONSTRAINT_HINTS

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
_COMPOSED_KEYS = frozenset(
    {"$ref", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentRequired"}
)
_INSTRUCTION = "请按已公布的input_schema修正类型、范围及必填字段。"
_MAX_TYPE_MISMATCHES = 8
_NUMERIC_BOUNDS = (
    ("minimum", "≥"),
    ("exclusiveMinimum", ">"),
    ("maximum", "≤"),
    ("exclusiveMaximum", "<"),
)

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


def _declared_numeric_bounds(
    schema: Mapping[str, JsonValue],
) -> list[tuple[str, str, int | float]] | None:
    bounds: list[tuple[str, str, int | float]] = []
    for key, symbol in _NUMERIC_BOUNDS:
        if key not in schema:
            continue
        bound = schema[key]
        if (
            not isinstance(bound, (int, float))
            or type(bound) not in {int, float}
            or abs(bound) > 10**18
            or (type(bound) is float and not math.isfinite(bound))
        ):
            return None
        bounds.append((key, symbol, bound))
    lower = [bound for key, _, bound in bounds if key in {"minimum", "exclusiveMinimum"}]
    upper = [bound for key, _, bound in bounds if key in {"maximum", "exclusiveMaximum"}]
    if lower and upper and max(lower) > min(upper):
        return None
    return bounds


def _range_mismatches(
    properties: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]
) -> list[str]:
    """只投影直接数值合同的声明范围；不泄露实际值，不猜测复合规则。"""
    mismatches: list[str] = []
    for name in sorted(properties):
        schema = properties[name]
        value = arguments.get(name)
        if (
            type(schema) is not dict
            or type(schema.get("type")) is not str
            or schema.get("type") not in {"integer", "number"}
            or any(key in schema for key in _COMPOSED_KEYS)
            or not isinstance(value, (int, float))
            or type(value) not in {int, float}
            or (type(value) is float and not math.isfinite(value))
            or (schema.get("type") == "integer" and type(value) is not int)
        ):
            continue
        bounds = _declared_numeric_bounds(schema)
        if not bounds:
            continue
        outside = any(
            (key == "minimum" and value < bound)
            or (key == "exclusiveMinimum" and value <= bound)
            or (key == "maximum" and value > bound)
            or (key == "exclusiveMaximum" and value >= bound)
            for key, _, bound in bounds
        )
        if outside:
            declared = "、".join(f"{symbol} {bound}" for _, symbol, bound in bounds)
            mismatches.append(f"{name} 应满足 {declared}")
            if len(mismatches) == _MAX_TYPE_MISMATCHES:
                break
    return mismatches


# 内置结构错误的文本只含合同约束、不含输入值；harnessix_前缀为本项目固定措辞的自定义错误。
_STRUCTURAL_ERROR_TYPES = frozenset(
    {
        "missing",
        "extra_forbidden",
        "literal_error",
        "enum",
        "string_type",
        "int_type",
        "int_parsing",
        "bool_type",
        "float_type",
        "list_type",
        "tuple_type",
        "dict_type",
        "model_type",
        "too_short",
        "too_long",
        "string_too_short",
        "string_too_long",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
    }
)


def _resolve(schema: JsonValue, root: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    """只解析本地#/$defs引用；其他引用或组合不猜测。"""
    for _ in range(8):
        if type(schema) is not dict:
            return None
        reference = schema.get("$ref")
        if reference is None:
            return schema
        if type(reference) is not str or not reference.startswith("#/$defs/"):
            return None
        definitions = root.get("$defs")
        if type(definitions) is not dict:
            return None
        schema = definitions.get(reference[len("#/$defs/") :])
    return None


def _schema_location(loc: Sequence[Any], root: Mapping[str, JsonValue]) -> str | None:
    """位置须沿已公布input_schema逐级解析（对象属性、数组元素），确保只来自公开合同。"""
    node: Mapping[str, JsonValue] | None = root
    parts: list[str] = []
    for part in loc:
        if node is None:
            return None
        if type(part) is int and 0 <= part < 10_000:
            if node.get("type") != "array":
                return None
            node = _resolve(node.get("items"), root)
            parts.append(f"[{part}]")
        elif type(part) is str and _FIELD_NAME.fullmatch(part):
            properties = node.get("properties")
            if type(properties) is not dict or part not in properties:
                return None
            node = _resolve(properties[part], root)
            parts.append(f".{part}" if parts else part)
        else:
            return None
    return "".join(parts) or None


def _validation_details(
    errors: Sequence[Mapping[str, Any]], schema: Mapping[str, JsonValue]
) -> list[str]:
    """按白名单投影校验错误位置与固定文本；被子位置错误覆盖的父级汇总错误不重复报告。"""
    located: list[tuple[tuple[Any, ...], str, str]] = []
    for error in errors:
        loc = tuple(error.get("loc", ()))
        where = _schema_location(loc, schema)
        if where is None:
            continue
        kind = error.get("type")
        message = error.get("msg")
        if (
            type(kind) is str
            and type(message) is str
            and (kind in _STRUCTURAL_ERROR_TYPES or kind.startswith("harnessix_"))
        ):
            located.append((loc, where, message[:200]))
        else:
            located.append((loc, where, "取值不符合合同"))
    details: list[str] = []
    for loc, where, message in located:
        if any(other != loc and other[: len(loc)] == loc for other, _, _ in located):
            continue
        line = f"{where}：{message}"
        if line not in details:
            details.append(line)
        if len(details) == _MAX_TYPE_MISMATCHES:
            break
    return details


def invalid_argument_message(
    schema: Mapping[str, JsonValue],
    arguments: Mapping[str, JsonValue],
    *,
    trusted_action: bool = False,
    constraint_codes: tuple[str, ...] = (),
    validation_errors: Sequence[Mapping[str, Any]] = (),
) -> str:
    """只公开Schema事实及正式校验器的白名单规则；参数值及异常不能进入消息。"""
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
    ranges = _range_mismatches(properties, arguments)
    details = []
    if mismatches:
        details.append(f"类型不匹配：{'；'.join(mismatches)}。")
    if ranges:
        details.append(f"范围不匹配：{'；'.join(ranges)}。")
    constraints = sorted(
        {
            READ_ARGUMENT_CONSTRAINT_HINTS[code]
            for code in constraint_codes[:_MAX_TYPE_MISMATCHES]
            if code in READ_ARGUMENT_CONSTRAINT_HINTS
        }
    )
    if constraints:
        details.append(f"约束不匹配：{'；'.join(constraints)}。")
    located = _validation_details(validation_errors, schema)
    if located:
        details.append(f"校验位置：{'；'.join(located)}。")
    message = f"{field_feedback}{''.join(details)}{instruction}" if details else previous_message
    if len(message) <= 2000:
        return message
    return previous_message if len(previous_message) <= 2000 else fallback
