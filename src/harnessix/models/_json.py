from __future__ import annotations

import json

from pydantic import JsonValue

from harnessix.models._bounded_http import InvalidWireData


def _object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidWireData("工具参数含重复 JSON key")
        result[key] = value
    return result


def _non_finite(value: str) -> None:
    raise InvalidWireData("工具参数含非有限数值")


def strict_json(value: str | bytes) -> JsonValue:
    result: JsonValue = json.loads(value, object_pairs_hook=_object, parse_constant=_non_finite)
    json.dumps(result, allow_nan=False)
    return result
