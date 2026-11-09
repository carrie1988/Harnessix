"""共享字段反馈的有界性、降级语义及不回显输入回归。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from harnessix.delivery.trusted_action import workspace_patch_descriptor
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.tools.argument_feedback import invalid_argument_message
from harnessix.tools.contracts import READ_ARGUMENT_CONSTRAINT_HINTS


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


def test_constraint_feedback_only_uses_deduplicated_static_whitelist():
    known = "harnessix_read_anchor_single_line"
    codes = ("PRIVATE_UNREGISTERED_CODE", known, known)
    message = invalid_argument_message(
        schema(), {"profile": "PRIVATE_INPUT_VALUE"}, constraint_codes=codes
    )
    assert message.count(READ_ARGUMENT_CONSTRAINT_HINTS[known]) == 1
    assert "PRIVATE" not in message and len(message) <= 2000
    assert codes == ("PRIVATE_UNREGISTERED_CODE", known, known)


def test_unknown_validation_codes_preserve_original_feedback():
    arguments = {"profile": "PRIVATE_VALUE"}
    assert invalid_argument_message(
        schema(), arguments, constraint_codes=("value_error", "PRIVATE_ERROR_CODE")
    ) == invalid_argument_message(schema(), arguments)


def test_unprojectable_schema_does_not_publish_constraint_hints():
    composed = {**schema(), "allOf": []}
    assert invalid_argument_message(
        composed, {}, constraint_codes=tuple(READ_ARGUMENT_CONSTRAINT_HINTS)
    ) == invalid_argument_message(composed, {})


def test_constraint_feedback_preserves_message_cap_and_immutable_rules():
    properties = {f"f{i:02}" + "a" * 60: {} for i in range(64)}
    long_schema = {"type": "object", "properties": properties, "required": list(properties)}
    message = invalid_argument_message(
        long_schema, {}, constraint_codes=tuple(READ_ARGUMENT_CONSTRAINT_HINTS)
    )
    assert len(message) <= 2000 and "允许字段：" not in message
    with pytest.raises(TypeError):
        READ_ARGUMENT_CONSTRAINT_HINTS["PRIVATE_NEW_RULE"] = "PRIVATE_TEXT"  # type: ignore[index]


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


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize(
    "bound,symbol,value",
    [
        ("minimum", "≥", -987654321),
        ("maximum", "≤", 987654321),
        ("exclusiveMinimum", ">", 0),
        ("exclusiveMaximum", "<", 0),
    ],
)
def test_direct_numeric_range_reports_only_registered_limit(trusted, bound, symbol, value):
    registered = {
        "type": "object",
        "properties": {"count": {"type": "integer", bound: 0}},
        "required": ["count"],
    }
    arguments = {"count": value, "PRIVATE_FIELD": "PRIVATE_VALUE"}
    frozen = deepcopy((registered, arguments))
    message = invalid_argument_message(registered, arguments, trusted_action=trusted)
    assert f"范围不匹配：count 应满足 {symbol} 0。" in message
    assert "987654321" not in message and "PRIVATE" not in message
    assert (registered, arguments) == frozen


@pytest.mark.parametrize("value", [0, 1, 2, 3])
def test_grep_range_boundaries_preserve_original_message_when_valid(value):
    from harnessix.tools.search_contracts import GrepInput

    registered = GrepInput.model_json_schema()
    message = invalid_argument_message(
        registered, {"query": "PRIVATE_VALUE", "context_lines": value}
    )
    assert "范围不匹配" not in message and "PRIVATE" not in message


def test_grep_invalid_context_has_concrete_repair_bounds_without_input_value():
    from harnessix.tools.search_contracts import GrepInput

    arguments = {"query": "PRIVATE_QUERY", "context_lines": 987654321}
    with pytest.raises(ValidationError):
        GrepInput.model_validate(arguments)
    message = invalid_argument_message(GrepInput.model_json_schema(), arguments)
    assert "context_lines 应满足 ≥ 0、≤ 3" in message
    assert "987654321" not in message and "PRIVATE" not in message


@pytest.mark.parametrize(
    "property_schema",
    [
        {"type": "integer", "maximum": "PRIVATE_LIMIT"},
        {"type": "integer", "maximum": True},
        {"type": "integer", "maximum": float("nan")},
        {"type": "integer", "maximum": float("inf")},
        {"type": "integer", "maximum": 10**100},
        {"type": "integer", "minimum": 4, "maximum": 3},
        {"type": "integer", "maximum": 3, "$ref": "PRIVATE_REF"},
        {"type": ["integer", "null"], "maximum": 3},
        {"anyOf": [{"type": "integer", "maximum": 3}, {"type": "null"}]},
    ],
)
def test_ambiguous_or_unsafe_ranges_keep_safe_fallback(property_schema):
    message = invalid_argument_message(
        {"type": "object", "properties": {"count": property_schema}}, {"count": 987654321}
    )
    assert "范围不匹配" not in message and "PRIVATE" not in message
    assert "987654321" not in message


@pytest.mark.parametrize("value", [True, "PRIVATE_VALUE", [], None, float("nan"), float("inf")])
def test_wrong_or_nonfinite_value_is_not_coerced_into_numeric_feedback(value):
    message = invalid_argument_message(
        {"type": "object", "properties": {"count": {"type": "integer", "maximum": 3}}},
        {"count": value},
    )
    assert "范围不匹配" not in message and "PRIVATE" not in message


def test_number_range_and_detail_limit_remain_bounded():
    registered = {
        "type": "object",
        "properties": {
            f"count_{i:02}": {"type": "number", "exclusiveMaximum": 0.5}
            for i in reversed(range(12))
        },
    }
    message = invalid_argument_message(
        registered, {f"count_{i:02}": 987654321.5 for i in range(12)}
    )
    details = message.split("范围不匹配：", 1)[1].split("。", 1)[0]
    assert details.split("；") == [f"count_{i:02} 应满足 < 0.5" for i in range(8)]
    assert len(message) <= 2000 and "987654321" not in message


def test_nested_patch_errors_name_the_location_and_contract_without_values():
    """d4：create缺mode时只报“缺少必填字段：无”，DeepSeek连续50次盲目重试。"""
    from pydantic import ValidationError

    from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
    from harnessix.delivery.workspace_patch_compact import compact_workspace_patch_schema

    arguments = {"files": [{"operation": "create", "path": "a.py", "content": "SECRET_BODY"}]}
    try:
        WorkspacePatchInput.model_validate(arguments)
    except ValidationError as error:
        errors = error.errors(include_url=False, include_input=False, include_context=False)
    message = invalid_argument_message(
        compact_workspace_patch_schema(),
        arguments,
        trusted_action=True,
        validation_errors=errors,
    )
    assert "files[0]：create需要path、content、mode" in message
    assert "too_short" not in message and "Tuple should have" not in message
    assert "SECRET_BODY" not in message


def test_unlisted_error_text_and_unsafe_keys_are_not_projected():
    errors = [
        {"loc": ("files", 0, "path"), "type": "value_error", "msg": "Value error, leaked VALUE"},
        {"loc": ("files", "bad key!"), "type": "missing", "msg": "Field required"},
        {"loc": ("PRIVATE_FIELD",), "type": "missing", "msg": "Field required"},
    ]
    item = {"type": "object", "properties": {"path": {"type": "string"}}}
    schema = {
        "type": "object",
        "properties": {"files": {"type": "array", "items": item}},
        "required": ["files"],
    }
    message = invalid_argument_message(schema, {"files": []}, validation_errors=errors)
    assert "files[0].path：取值不符合合同" in message
    assert "leaked VALUE" not in message and "bad key!" not in message
    # 位置不在公开Schema内（如校验器内部字段）时整条不报告。
    assert "PRIVATE_FIELD" not in message


def test_workspace_patch_no_change_edit_reports_the_fixed_reason_without_text() -> None:
    arguments = {
        "files": [
            {
                "operation": "edit",
                "path": "src/example.py",
                "expected_sha256": "a" * 64,
                "edits": [{"old_text": "PRIVATE_SAME", "new_text": "PRIVATE_SAME"}],
            }
        ]
    }
    with pytest.raises(ValidationError) as error:
        WorkspacePatchInput.model_validate(arguments)
    for contract in ("standard", "compact"):
        message = invalid_argument_message(
            workspace_patch_descriptor(contract).input_schema,
            arguments,
            trusted_action=True,
            validation_errors=error.value.errors(
                include_url=False, include_input=False, include_context=False
            ),
        )
        assert "files[0].edits[0]：编辑必须改变内容：old_text与new_text完全相同" in message
        assert "PRIVATE_SAME" not in message and "after validation" not in message


def _patch_feedback(arguments: dict, contract: str = "standard") -> str:
    with pytest.raises(ValidationError) as error:
        WorkspacePatchInput.model_validate(arguments)
    return invalid_argument_message(
        workspace_patch_descriptor(contract).input_schema,
        arguments,
        trusted_action=True,
        validation_errors=error.value.errors(
            include_url=False, include_input=False, include_context=False
        ),
    )


def _patch_file(**extra: object) -> dict:
    return {
        "operation": "edit",
        "path": "src/example.py",
        "expected_sha256": "a" * 64,
        "edits": [{"old_text": "PRIVATE_OLD", "new_text": "PRIVATE_NEW"}],
        **extra,
    }


@pytest.mark.parametrize("contract", ["standard", "compact"])
def test_extra_file_field_is_named_instead_of_the_derived_empty_files_error(contract) -> None:
    """holdout实测：files[0]多带content_sha256:null，旧消息只说files须至少1项（全部项无效后的0项）。"""
    message = _patch_feedback({"files": [_patch_file(content_sha256=None)]}, contract)
    assert (
        "校验位置：files[0].content_sha256：合同未定义该字段，请删除"
        "（此处允许字段：content, edits, expected_sha256, mode, operation, path）。"
    ) in message
    assert "Tuple should have" not in message and "after validation" not in message
    assert "PRIVATE" not in message and "None" not in message and "null" not in message
    assert len(message) <= 2000


@pytest.mark.parametrize("contract", ["standard", "compact"])
def test_extra_edit_field_is_located_through_the_nullable_edits_schema(contract) -> None:
    """标准Schema的edits是“数组或null”（anyOf）；位置须穿过该可空包装才能定位到编辑项。"""
    arguments = {
        "files": [
            _patch_file(
                edits=[
                    {"old_text": "PRIVATE_OLD", "new_text": "PRIVATE_NEW", "new_text_note": None}
                ]
            )
        ]
    }
    message = _patch_feedback(arguments, contract)
    assert (
        "校验位置：files[0].edits[0].new_text_note：合同未定义该字段，请删除"
        "（此处允许字段：new_text, old_text）。"
    ) in message
    assert "after validation" not in message and "PRIVATE" not in message


def test_repeated_extra_fields_name_each_location_and_list_allowed_fields_once() -> None:
    files = [{**_patch_file(content_sha256=None), "path": f"src/f{i}.py"} for i in range(3)]
    message = _patch_feedback({"files": files})
    located = message.split("校验位置：", 1)[1].split("。", 1)[0].split("；")
    assert located == [
        "files[0].content_sha256：合同未定义该字段，请删除"
        "（此处允许字段：content, edits, expected_sha256, mode, operation, path）",
        "files[1].content_sha256：合同未定义该字段，请删除",
        "files[2].content_sha256：合同未定义该字段，请删除",
    ]


def test_top_level_extra_field_is_named_with_the_published_fields() -> None:
    message = _patch_feedback({"files": [_patch_file()], "dry_run": True})
    assert "校验位置：dry_run：合同未定义该字段，请删除（此处允许字段：files, spec_version）。" in (
        message
    )


def test_empty_files_still_reports_the_genuine_length_error() -> None:
    message = _patch_feedback({"files": []})
    assert "校验位置：files：Tuple should have at least 1 item after validation, not 0。" in message


def test_unsafe_extra_names_are_not_echoed_and_do_not_expose_the_derived_parent_error() -> None:
    """名称不符合标识符规则时不回显；其父级too_short只是结果，也不能作为原因报告。"""
    message = _patch_feedback({"files": [_patch_file(**{"PRIVATE key!": "PRIVATE_VALUE"})]})
    assert "校验位置" not in message
    assert "PRIVATE" not in message and "after validation" not in message
    assert "允许字段：files, spec_version。" in message


def test_parent_summary_is_suppressed_by_unlocatable_child_errors() -> None:
    schema = {
        "type": "object",
        "properties": {"files": {"type": "array", "items": {"type": "object"}}},
        "required": ["files"],
    }
    errors = [
        {"loc": ("files", 0, "union_tag", "x"), "type": "missing", "msg": "Field required"},
        {"loc": ("files",), "type": "too_short", "msg": "Tuple should have at least 1 item"},
    ]
    message = invalid_argument_message(schema, {"files": [{}]}, validation_errors=errors)
    assert "校验位置" not in message and "Tuple should have" not in message
