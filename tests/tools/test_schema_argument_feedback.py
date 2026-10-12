"""共享字段反馈的有界性、降级语义及不回显输入回归。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harnessix.delivery.trusted_action import workspace_patch_descriptor
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
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


@pytest.mark.parametrize(
    ("declared_type", "actual", "actual_type"),
    [
        ("string", ["PRIVATE_VALUE"], "array"),
        ("array", "PRIVATE_VALUE", "string"),
        ("object", True, "boolean"),
        ("boolean", 1, "integer"),
        ("integer", True, "boolean"),
        ("number", None, "null"),
        ("null", {"PRIVATE_FIELD": "PRIVATE_VALUE"}, "object"),
    ],
)
def test_known_top_level_json_type_mismatch_is_reported_without_values(
    declared_type: str, actual: object, actual_type: str
) -> None:
    message = invalid_argument_message(
        {
            "type": "object",
            "properties": {"value": {"type": declared_type}},
            "required": ["value"],
        },
        {"value": actual},
    )
    assert f"value 应为 {declared_type}，实际为 {actual_type}" in message
    assert "PRIVATE" not in message


@pytest.mark.parametrize(
    ("declared_type", "actual"),
    [
        ("string", "value"),
        ("array", []),
        ("object", {}),
        ("boolean", False),
        ("integer", 1),
        ("number", 1),
        ("number", 1.5),
        ("null", None),
    ],
)
def test_compatible_json_types_do_not_add_type_feedback(declared_type: str, actual: object) -> None:
    message = invalid_argument_message(
        {"type": "object", "properties": {"value": {"type": declared_type}}},
        {"value": actual},
    )
    assert "类型不匹配：" not in message


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf"])
def test_simple_composed_types_are_merged(keyword: str) -> None:
    message = invalid_argument_message(
        {
            "type": "object",
            "properties": {"value": {keyword: [{"type": "null"}, {"type": "string"}]}},
        },
        {"value": []},
    )
    assert "value 应为 string 或 null，实际为 array" in message


def test_complex_property_schema_silently_keeps_existing_feedback() -> None:
    message = invalid_argument_message(
        {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "string"},
                        {"$ref": "#/$defs/PRIVATE_SCHEMA"},
                    ]
                }
            },
        },
        {"value": ["PRIVATE_VALUE"]},
    )
    assert "类型不匹配：" not in message
    assert "PRIVATE" not in message


def test_type_mismatch_feedback_is_sorted_and_count_bounded() -> None:
    names = [f"field_{index:02}" for index in reversed(range(12))]
    message = invalid_argument_message(
        {
            "type": "object",
            "properties": {name: {"type": "array"} for name in names},
        },
        {name: "PRIVATE_VALUE" for name in names},
    )
    details = message.split("类型不匹配：", maxsplit=1)[1].split("。", maxsplit=1)[0]
    assert details.split("；") == [
        f"field_{index:02} 应为 array，实际为 string" for index in range(8)
    ]
    assert "PRIVATE" not in message
    assert len(message) <= 2000


def test_workspace_patch_files_string_is_rejected_with_concrete_safe_feedback() -> None:
    arguments = {"files": "PRIVATE_PATCH_CONTENT"}
    definition = workspace_patch_descriptor()

    with pytest.raises(ValidationError):
        WorkspacePatchInput.model_validate(arguments)

    message = invalid_argument_message(definition.input_schema, arguments, trusted_action=True)
    assert message.startswith("Action参数不符合Trusted Tool契约")
    assert "files 应为 array，实际为 string" in message
    assert "PRIVATE_PATCH_CONTENT" not in message
    assert len(message) <= 2000
