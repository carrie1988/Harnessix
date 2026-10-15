"""按已公布input_schema还原被模型字符串化的JSON容器参数；不放宽任何校验。

部分模型会把数组/对象参数序列化为JSON字符串（如``"files": "[...]"``），偶尔还遗漏末尾的
闭合括号。仅当Schema在该位置明确声明为array或object、且字符串可严格解析（必要时仅补齐末尾
缺失的闭合括号）为同类型容器时才还原；其余值原样保留，由工具原有的严格校验照旧拒绝。
还原后的规范参数才进入审批、审计与执行。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue

from harnessix.models._json import strict_json

_MAX_DEPTH = 16


def normalize_stringified_containers(
    arguments: dict[str, JsonValue], schema: Mapping[str, Any]
) -> dict[str, JsonValue]:
    normalized = _normalize(arguments, schema, schema, 0)
    assert isinstance(normalized, dict)
    return normalized


def _normalize(value: JsonValue, node: Mapping[str, Any], root: Mapping[str, Any], depth: int):
    node = _resolve(node, root)
    if depth > _MAX_DEPTH or node is None:
        return value
    expected = _container_type(node)
    if isinstance(value, str) and expected is not None:
        value = _decoded(value, expected)
    if isinstance(value, dict):
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return value
        return {
            key: _normalize(item, properties[key], root, depth + 1)
            if isinstance(properties.get(key), Mapping)
            else item
            for key, item in value.items()
        }
    if isinstance(value, list):
        items = node.get("items")
        if not isinstance(items, Mapping):
            return value
        return [_normalize(item, items, root, depth + 1) for item in value]
    return value


def _resolve(node: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for _ in range(_MAX_DEPTH):
        reference = node.get("$ref")
        if reference is None:
            return node
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            return None
        definitions = root.get("$defs")
        target = (
            definitions.get(reference.removeprefix("#/$defs/"))
            if isinstance(definitions, Mapping)
            else None
        )
        if not isinstance(target, Mapping):
            return None
        node = target
    return None


def _container_type(node: Mapping[str, Any]) -> type | None:
    declared = node.get("type")
    if declared == "array":
        return list
    if declared == "object":
        return dict
    return None


def _decoded(text: str, expected: type) -> JsonValue:
    candidate = text.strip()
    for attempt in (candidate, _with_missing_closers(candidate)):
        if attempt is None:
            continue
        try:
            parsed = strict_json(attempt)
        except ValueError:
            continue
        if isinstance(parsed, expected):
            return parsed
    return text


def _with_missing_closers(text: str) -> str | None:
    """只补齐末尾缺失的外层闭合括号：文本须以已闭合的内层容器结尾（`]`或`}`）。

    不改写任何已有字符；值中途截断、未闭合字符串、多余闭合或其他语法错误一律放弃，
    交由严格校验拒绝。
    """
    if not text.endswith(("]", "}")):
        return None
    stack: list[str] = []
    in_string = escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            stack.append("]" if character == "[" else "}")
        elif character in "]}":
            if not stack or stack.pop() != character:
                return None
    if in_string or not stack:
        return None
    return text + "".join(reversed(stack))
