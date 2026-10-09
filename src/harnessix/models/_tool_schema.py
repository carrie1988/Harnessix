"""Provider 临时输入 Schema：去除展示标题，不改变验证合同或字面数据。"""

from __future__ import annotations

from copy import deepcopy

from pydantic import JsonValue

_SCHEMA_MAPS = frozenset(
    {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas", "dependencies"}
)
_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_CHILDREN = frozenset(
    {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "additionalItems",
        "unevaluatedItems",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_KNOWN_DIALECTS = frozenset(
    {
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        "https://json-schema.org/draft/2020-12/schema",
    }
)


def provider_tool_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """仅访问标准 Schema 位置；名为 title 的属性及 const/default 等数据必须保留。"""
    projected = deepcopy(schema)
    pending: list[dict[str, JsonValue]] = [projected]
    while pending:
        node = pending.pop()
        # 不猜测自定义方言/词汇，也不把类型无效的标题洗成合法 Schema。
        dialect = node.get("$schema")
        if "$vocabulary" in node or (
            "$schema" in node and (not isinstance(dialect, str) or dialect not in _KNOWN_DIALECTS)
        ):
            continue
        if isinstance(node.get("title"), str):
            node.pop("title")
        for key, value in node.items():
            children: list[JsonValue] = []
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                children = list(value.values())
            elif key in _SCHEMA_LISTS and isinstance(value, list):
                children = value
            elif key in _SCHEMA_CHILDREN:
                # Draft 7 的 tuple items 是 Schema 数组；字符串依赖列表不是。
                children = value if key == "items" and isinstance(value, list) else [value]
            pending.extend(child for child in children if isinstance(child, dict))
    return projected
