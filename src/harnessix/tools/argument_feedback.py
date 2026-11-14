"""从注册输入合同生成有界字段反馈，不回显调用值或校验异常正文。

反馈写给模型，使用英文；界面按错误码另行展示用户文案（product_ui.tool_summary）。
"""

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
_INSTRUCTION = "Fix the listed fields to match the published input_schema and resend the call."
_NO_EXTRA_FIELDS = " Fields outside the schema are rejected."
_EXTRA_FIELD_MESSAGE = "not a field of this contract, remove it"
_UNSPECIFIED_MESSAGE = "value does not satisfy the contract"
_MAX_TYPE_MISMATCHES = 8
_MAX_DETAIL_CHARS = 320
_NUMERIC_BOUNDS = (
    ("minimum", ">="),
    ("exclusiveMinimum", ">"),
    ("maximum", "<="),
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
        mismatch = f"{name} must be {' or '.join(expected)}, got {actual}"
        if actual == "string" and {"array", "object"} & set(expected):
            # 字符串化容器无法严格还原时走到这里：多半是字符串里的JSON本身不合法。
            mismatch += " (send the JSON value itself, not a string that contains JSON)"
        mismatches.append(mismatch)
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
            declared = " and ".join(f"{symbol} {bound}" for _, symbol, bound in bounds)
            mismatches.append(f"{name} must be {declared}")
            if len(mismatches) == _MAX_TYPE_MISMATCHES:
                break
    return mismatches


# 内置结构错误的文本只含合同约束、不含输入值；harnessix_前缀为本项目固定措辞的自定义错误。
_STRUCTURAL_ERROR_TYPES = frozenset(
    {
        "missing",
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


def _non_null_branch(
    node: Mapping[str, JsonValue], root: Mapping[str, JsonValue]
) -> Mapping[str, JsonValue] | None:
    """可空字段（X或null）的anyOf/oneOf只有一个非null分支时进入该分支；其他组合不猜测。"""
    if any(key in node for key in ("type", "properties", "items")):
        return None
    keys = [key for key in ("anyOf", "oneOf") if key in node]
    options = node.get(keys[0]) if len(keys) == 1 else None
    if type(options) is not list or len(options) != 2:
        return None
    branches = [_resolve(option, root) for option in options]
    if any(branch is None for branch in branches) or {"type": "null"} not in branches:
        return None
    concrete = [branch for branch in branches if branch != {"type": "null"}]
    return concrete[0] if len(concrete) == 1 else None


def _schema_node(
    loc: Sequence[Any], root: Mapping[str, JsonValue]
) -> tuple[str, Mapping[str, JsonValue]] | None:
    """位置须沿已公布input_schema逐级解析（对象属性、数组元素、可空包装），确保只来自公开合同。"""
    node: Mapping[str, JsonValue] | None = root
    parts: list[str] = []
    for part in loc:
        if node is None:
            return None
        node = _non_null_branch(node, root) or node
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
    if node is None:
        return None
    return "".join(parts), _non_null_branch(node, root) or node


def _schema_location(loc: Sequence[Any], root: Mapping[str, JsonValue]) -> str | None:
    found = _schema_node(loc, root)
    if found is None or not found[0]:
        return None
    return found[0]


def _extra_field(loc: Sequence[Any], root: Mapping[str, JsonValue]) -> tuple[str, str] | None:
    """多余字段的父对象须在公开合同内；只回显符合标识符规则的字段名，不回显值。

    返回位置及该对象的允许字段提示；提示为空表示字段元数据不适合投影。
    """
    if not loc or type(loc[-1]) is not str or _FIELD_NAME.fullmatch(loc[-1]) is None:
        return None
    parent = _schema_node(loc[:-1], root)
    if parent is None:
        return None
    path, node = parent
    properties = node.get("properties")
    if type(properties) is not dict or loc[-1] in properties:
        return None
    where = f"{path}.{loc[-1]}" if path else loc[-1]
    allowed = sorted(properties)
    if (
        not allowed
        or len(allowed) > 64
        or any(_FIELD_NAME.fullmatch(name) is None for name in allowed)
    ):
        return where, ""
    return where, f"allowed here: {', '.join(allowed)}"


def _whitelisted_text(kind: Any, message: Any) -> str | None:
    """只有内置结构错误与本项目固定措辞的harnessix_错误可原样公开；其余返回None。"""
    if type(kind) is not str or type(message) is not str:
        return None
    if kind in _STRUCTURAL_ERROR_TYPES or kind.startswith("harnessix_"):
        return message[:_MAX_DETAIL_CHARS]
    return None


def _validation_details(
    errors: Sequence[Mapping[str, Any]], schema: Mapping[str, JsonValue]
) -> list[str]:
    """按白名单投影校验错误位置与固定文本。

    父级汇总错误（如全部元素无效后“0个有效元素”的too_short）只是子位置错误的结果：只要存在
    更深位置的错误就不报告父级，即使子位置因不在公开合同内而不能报告，以免给出误导原因。
    """
    locations = [tuple(error.get("loc", ())) for error in errors]
    # (位置元组, 公开位置, 固定文本, 同一对象只提示一次的允许字段)
    located: list[tuple[tuple[Any, ...], str, str, str]] = []
    for loc, error in zip(locations, errors, strict=True):
        kind = error.get("type")
        if kind == "extra_forbidden":
            extra = _extra_field(loc, schema)
            if extra is not None:
                located.append((loc, extra[0], _EXTRA_FIELD_MESSAGE, extra[1]))
            continue
        text = _whitelisted_text(kind, error.get("msg"))
        if not loc:
            # 整个调用级的规则（如重复路径）没有字段位置，白名单正文自带所涉下标。
            if text is not None:
                located.append((loc, "", text, ""))
            continue
        where = _schema_location(loc, schema)
        if where is not None:
            located.append((loc, where, text or _UNSPECIFIED_MESSAGE, ""))
    details: list[str] = []
    hints: set[str] = set()
    for loc, where, message, hint in located:
        if any(len(other) > len(loc) and other[: len(loc)] == loc for other in locations):
            continue
        line = f"{where}: {message}" if where else message
        if hint and hint not in hints:
            hints.add(hint)
            line = f"{line} ({hint})"
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
    label = (
        "Action arguments do not match the trusted tool contract"
        if trusted_action
        else "Tool arguments do not match the contract"
    )
    fallback = f"{label}. {_INSTRUCTION}"
    fields = _field_facts(schema)
    if fields is None:
        return fallback
    allowed, required, properties = fields
    missing = [name for name in required if name not in arguments]
    instruction = _INSTRUCTION + (
        _NO_EXTRA_FIELDS if schema.get("additionalProperties") is False else ""
    )
    field_feedback = (
        f"Required fields: {', '.join(required) or 'none'}; "
        f"missing required fields: {', '.join(missing) or 'none'}; "
        f"allowed fields: {', '.join(allowed) or 'none'}. "
    )
    previous_message = f"{label}. {field_feedback}{instruction}"
    # 具体问题排在字段清单之前：模型先读到要改哪一处，再读合同事实。
    details = []
    located = _validation_details(validation_errors, schema)
    if located:
        details.append(f"Problems: {'; '.join(located)}. ")
    mismatches = _type_mismatches(properties, arguments)
    if mismatches:
        details.append(f"Type mismatch: {'; '.join(mismatches)}. ")
    ranges = _range_mismatches(properties, arguments)
    if ranges:
        details.append(f"Out of range: {'; '.join(ranges)}. ")
    constraints = sorted(
        {
            READ_ARGUMENT_CONSTRAINT_HINTS[code]
            for code in constraint_codes[:_MAX_TYPE_MISMATCHES]
            if code in READ_ARGUMENT_CONSTRAINT_HINTS
        }
    )
    if constraints:
        details.append(f"Constraint: {'; '.join(constraints)}. ")
    if not details:
        return previous_message if len(previous_message) <= 2000 else fallback
    message = f"{label}. {''.join(details)}{field_feedback}{instruction}"
    if len(message) <= 2000:
        return message
    return previous_message if len(previous_message) <= 2000 else fallback
