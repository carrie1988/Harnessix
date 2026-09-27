"""纯公开输出合同：闭合JSON Schema子集、有界捕获及共享工作预算。"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

MAX_SCHEMA_BYTES = 32 * 1024
MAX_SCHEMA_DEPTH = 16
MAX_SCHEMA_NODES = 512
MAX_VALIDATION_WORK = 32768
MAX_ARRAY_ITEMS = 256
MAX_STRING_LENGTH = 4096
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_ANNOTATIONS = frozenset({"title", "description"})
_COMMON = _ANNOTATIONS | {"type", "enum", "const", "$schema"}
_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}


def capture_public_output_schema(value: object) -> dict[str, JsonValue]:
    """先检查原生树，再序列化；复制合同，不执行引用、正则或任意对象钩子。"""

    _inspect_native_schema(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError("公开输出Schema超过字节上限")
    schema = cast(dict[str, JsonValue], json.loads(encoded))
    _check_schema(schema, root=True)
    return schema


def _inspect_native_schema(value: object) -> None:
    stack = [(value, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > MAX_SCHEMA_DEPTH or count > MAX_SCHEMA_NODES:
            raise ValueError("公开输出Schema超过结构上限")
        if type(node) is dict:
            if len(node) > 128:
                raise ValueError("公开输出Schema对象过大")
            for key, child in node.items():
                if type(key) is not str or len(key) > 128:
                    raise ValueError("公开输出Schema键无效")
                stack.append((child, depth + 1))
        elif type(node) is list:
            if len(node) > 128:
                raise ValueError("公开输出Schema数组过大")
            stack.extend((child, depth + 1) for child in node)
        elif type(node) is str:
            if len(node) > MAX_STRING_LENGTH:
                raise ValueError("公开输出Schema字符串过大")
        elif type(node) is int:
            if node.bit_length() > 128:
                raise ValueError("公开输出Schema整数过大")
        elif type(node) is float:
            if not math.isfinite(node):
                raise ValueError("公开输出Schema数字无效")
        elif node is not None and type(node) is not bool:
            raise ValueError("公开输出Schema必须是原生JSON")


def _integer_bound(schema: dict[str, JsonValue], key: str, limit: int, default: int = 0) -> int:
    value = schema.get(key, default)
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError("公开输出Schema界限无效")
    return value


def _check_schema(schema: object, *, root: bool = False) -> None:
    if type(schema) is not dict:
        raise ValueError("公开输出Schema节点必须是对象")
    if "anyOf" in schema:
        _check_choices(schema, root)
        return
    kind = schema.get("type")
    if type(kind) is not str or kind not in _TYPES or (root and kind != "object"):
        raise ValueError("公开输出Schema类型无效")
    if set(schema) - (_COMMON | _KEYWORDS[kind]):
        raise ValueError("公开输出Schema关键词不受支持")
    _check_literals(schema)
    _check_kind(schema, kind, root)


def _check_choices(schema: dict[str, JsonValue], root: bool) -> None:
    choices = schema["anyOf"]
    if root or set(schema) - (_ANNOTATIONS | {"anyOf"}) or type(choices) is not list:
        raise ValueError("公开输出Schema组合无效")
    if not 1 <= len(choices) <= 8:
        raise ValueError("公开输出Schema组合过大")
    _check_literals(schema)
    for choice in choices:
        _check_schema(choice)


def _check_literals(schema: dict[str, JsonValue]) -> None:
    if "$schema" in schema and schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("公开输出Schema方言不受支持")
    if any(type(schema[key]) is not str for key in _ANNOTATIONS & schema.keys()):
        raise ValueError("公开输出Schema注解无效")
    if "enum" in schema:
        values = schema["enum"]
        if type(values) is not list or not 1 <= len(values) <= 32:
            raise ValueError("公开输出Schema枚举无效")
        if any(type(value) in {dict, list} for value in values):
            raise ValueError("公开输出Schema枚举只允许标量")
    if "const" in schema and type(schema["const"]) in {dict, list}:
        raise ValueError("公开输出Schema常量只允许标量")


def _check_kind(schema: dict[str, JsonValue], kind: str, root: bool) -> None:
    if kind == "object":
        _check_object(schema, root)
    elif kind in {"array", "string"}:
        _check_sequence(schema, kind)
    elif kind in {"integer", "number"}:
        _check_numeric(schema)


def _check_object(schema: dict[str, JsonValue], root: bool) -> None:
    props = schema.get("properties")
    required = schema.get("required", [])
    if type(props) is not dict or schema.get("additionalProperties") is not False:
        raise ValueError("公开输出Schema对象必须封闭")
    if root and "artifact" in props:
        raise ValueError("公开输出Schema不能占用Artifact字段")
    if type(required) is not list or any(type(key) is not str for key in required):
        raise ValueError("公开输出Schema必需字段无效")
    if len(set(required)) != len(required) or set(required) - props.keys():
        raise ValueError("公开输出Schema必需字段不属于属性")
    for child in props.values():
        _check_schema(child)


def _check_sequence(schema: dict[str, JsonValue], kind: str) -> None:
    suffix, limit = ("Items", MAX_ARRAY_ITEMS) if kind == "array" else ("Length", MAX_STRING_LENGTH)
    if "max" + suffix not in schema:
        raise ValueError("公开输出Schema必须声明有限上限")
    maximum = _integer_bound(schema, "max" + suffix, limit)
    if _integer_bound(schema, "min" + suffix, limit) > maximum:
        raise ValueError("公开输出Schema上下限不一致")
    if kind == "array":
        _check_schema(schema.get("items"))


def _check_numeric(schema: dict[str, JsonValue]) -> None:
    for key in ("minimum", "maximum"):
        if key in schema and type(schema[key]) not in {int, float}:
            raise ValueError("公开输出Schema数值界限无效")
    if "minimum" in schema and "maximum" in schema:
        if cast(int | float, schema["minimum"]) > cast(int | float, schema["maximum"]):
            raise ValueError("公开输出Schema数值上下限不一致")


@dataclass
class _Work:
    checkpoint: Callable[[], None] | None
    remaining: int = MAX_VALIDATION_WORK

    def step(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ValueError("公开输出验证超过工作上限")
        if self.checkpoint is not None:
            self.checkpoint()


def validate_public_output(
    schema: dict[str, JsonValue], value: JsonValue, *, checkpoint: Callable[[], None] | None = None
) -> None:
    """严格验证原JSON；所有组合共用工作计数，不补默认值、不过滤或转换字段。"""

    checked = capture_public_output_schema(schema)
    if not _matches(checked, value, _Work(checkpoint, remaining=MAX_VALIDATION_WORK)):
        raise ValueError("正文不符合公开输出合同")


def _matches(schema: dict[str, JsonValue], value: JsonValue, work: _Work) -> bool:
    work.step()
    choices = schema.get("anyOf")
    if isinstance(choices, list):
        return any(_matches(cast(dict[str, JsonValue], choice), value, work) for choice in choices)
    kind = cast(str, schema["type"])
    if not _type_matches(kind, value):
        return False
    if "const" in schema and not _same_atom(schema["const"], value):
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_same_atom(choice, value) for choice in enum):
        return False
    return _content_matches(schema, kind, value, work)


def _content_matches(
    schema: dict[str, JsonValue], kind: str, value: JsonValue, work: _Work
) -> bool:
    if kind == "object":
        props = cast(dict[str, dict[str, JsonValue]], schema["properties"])
        fields = cast(dict[str, JsonValue], value)
        required = cast(list[str], schema.get("required", []))
        if fields.keys() - props.keys() or not set(required) <= fields.keys():
            return False
        return all(_matches(props[key], child, work) for key, child in fields.items())
    if kind == "array":
        items = cast(list[JsonValue], value)
        if not cast(int, schema.get("minItems", 0)) <= len(items) <= cast(int, schema["maxItems"]):
            return False
        return all(
            _matches(cast(dict[str, JsonValue], schema["items"]), child, work) for child in items
        )
    if kind == "string":
        return (
            cast(int, schema.get("minLength", 0))
            <= len(cast(str, value))
            <= cast(int, schema["maxLength"])
        )
    if kind in {"integer", "number"}:
        number = cast(int | float, value)
        return ("minimum" not in schema or number >= cast(int | float, schema["minimum"])) and (
            "maximum" not in schema or number <= cast(int | float, schema["maximum"])
        )
    return True


def _type_matches(kind: str, value: JsonValue) -> bool:
    if kind == "integer":
        return type(value) is int and value.bit_length() <= 128
    if kind == "number":
        return (type(value) is int and value.bit_length() <= 128) or (
            type(value) is float and math.isfinite(value)
        )
    return (
        type(value)
        is {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}[kind]
    )


def _same_atom(expected: JsonValue, value: JsonValue) -> bool:
    if type(expected) is bool or type(value) is bool:
        return type(expected) is type(value) and expected == value
    return expected == value
