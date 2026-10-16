"""模型Provider适配：执行严格JSON序列化与错误归一化。"""

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


def strict_json(value: str | bytes, *, control_characters: bool = False) -> JsonValue:
    result: JsonValue = json.loads(
        value,
        object_pairs_hook=_object,
        parse_constant=_non_finite,
        strict=not control_characters,
    )
    json.dumps(result, allow_nan=False)
    return result


_JSON_ESCAPES = frozenset('"\\/bfnrtu')


def _escape_stray_backslashes(text: str) -> str:
    """合法转义原样保留；其他字符前的反斜杠按字面量转义（模型写出的正则、路径等）。"""
    out: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\":
            following = text[index + 1 : index + 2]
            if following and following in _JSON_ESCAPES:
                out.append(text[index : index + 2])
                index += 2
                continue
            out.append("\\\\")
        else:
            out.append(character)
        index += 1
    return "".join(out)


def repaired_json_object(value: str) -> dict[str, JsonValue] | None:
    """仅修复两类唯一、保意图的错误：字符串内的原始控制字符与非法反斜杠转义。

    截断、括号不配、重复键或非有限数值不修复，返回None交由原严格路径拒绝。
    """
    for candidate in (value, _escape_stray_backslashes(value)):
        try:
            result = strict_json(candidate, control_characters=True)
        except ValueError:
            continue
        return result if isinstance(result, dict) else None
    return None
