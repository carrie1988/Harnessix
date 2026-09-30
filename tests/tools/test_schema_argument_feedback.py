"""共享字段反馈的有界性、降级语义及不回显输入回归。"""

from __future__ import annotations

import pytest

from harnessix.tools.argument_feedback import invalid_argument_message


def schema() -> dict:
    return {
        "type": "object",
        "properties": {"selectors": {}, "profile": {}},
        "required": ["profile"],
        "additionalProperties": False,
    }


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"selectors": []},
        {"profile": None},
        {"EXTRA_PRIVATE_FIELD": {"INNER_PRIVATE_FIELD": "PRIVATE_VALUE"}},
        {"profile": "PRIVATE_VALUE"},
        {"profile": ["PRIVATE_VALUE"]},
    ],
)
def test_only_schema_field_presence_is_reported(trusted: bool, arguments: dict) -> None:
    value = schema()
    value["properties"]["profile"] = {"description": "SCHEMA_PRIVATE_VALUE"}
    message = invalid_argument_message(value, arguments, trusted_action=trusted)
    assert "必填字段：profile；" in message
    assert f"缺少必填字段：{'无' if 'profile' in arguments else 'profile'}；" in message
    assert "允许字段：profile, selectors。" in message
    assert "禁止额外字段" in message
    assert "PRIVATE" not in message
    assert len(message) <= 2000


@pytest.mark.parametrize(
    "changed",
    [
        {"type": "array"},
        {"properties": None},
        {"required": None},
        {"required": ["profile", "profile"]},
        {"required": ["unregistered"]},
        {"required": [None]},
        {"properties": {"NAME_PRIVATE_VALUE\n": {}}},
        {"properties": {"a" * 65: {}}},
        {"properties": {f"f{i}": {} for i in range(65)}},
        {"required": ["profile"] * 65},
        {"$ref": "PRIVATE_VALUE"},
        {"allOf": []},
        {"anyOf": []},
        {"oneOf": []},
        {"not": {}},
        {"if": {}},
        {"then": {}},
        {"else": {}},
        {"dependentRequired": {}},
    ],
)
def test_unprojectable_schema_uses_safe_fallback(changed: dict) -> None:
    message = invalid_argument_message({**schema(), **changed}, {"EXTRA": "PRIVATE_VALUE"})
    assert "input_schema" in message
    assert "必填字段：" not in message
    assert "允许字段：" not in message
    assert "PRIVATE" not in message
    assert len(message) <= 2000


def test_long_but_valid_metadata_does_not_publish_partial_field_list() -> None:
    properties = {f"f{i:02}" + "a" * 60: {} for i in range(64)}
    message = invalid_argument_message(
        {"type": "object", "properties": properties, "required": list(properties)}, {}
    )
    assert len(message) <= 2000 and "允许字段：" not in message


def test_empty_and_optional_field_schema_are_distinct_from_unknown() -> None:
    empty = invalid_argument_message({"type": "object", "properties": {}}, {})
    optional = invalid_argument_message({"type": "object", "properties": {"optional": {}}}, {})
    assert "必填字段：无；缺少必填字段：无；允许字段：无。" in empty
    assert "必填字段：无；缺少必填字段：无；允许字段：optional。" in optional
    assert "禁止额外字段" not in empty


def test_schema_and_arguments_are_not_mutated() -> None:
    value = schema()
    arguments = {"selectors": ["PRIVATE_VALUE"]}
    invalid_argument_message(value, arguments)
    assert value == schema()
    assert arguments == {"selectors": ["PRIVATE_VALUE"]}
