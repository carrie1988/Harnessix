"""按已公布input_schema还原被模型字符串化的JSON容器参数；不放宽任何校验。

部分模型会把数组/对象参数序列化为JSON字符串（如``"files": "[...]"``）。仅当Schema
在该位置明确声明为array或object、且字符串可严格解析为同类型容器时才还原；其余值原样
保留，由工具原有的严格校验照旧拒绝。还原后的规范参数才进入审批、审计与执行。
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
    try:
        parsed = strict_json(text.strip())
    except ValueError:
        return text
    return parsed if isinstance(parsed, expected) else text
