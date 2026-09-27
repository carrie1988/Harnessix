"""有限公开输出Schema：闭合字段、严格值类型、原生预算和共享组合工作量。"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from harnessix.domain import public_output_schema as contracts
from harnessix.domain.models import ToolDescriptor
from tests.trusted_actions.test_agent_gateway import descriptor


def object_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def rich_schema():
    return object_schema(
        {
            "count": {"type": "integer", "minimum": 0, "maximum": 100},
            "message": {"anyOf": [{"type": "null"}, {"type": "string", "maxLength": 64}]},
            "items": {
                "type": "array",
                "maxItems": 4,
                "items": object_schema({"ok": {"type": "boolean"}}, ["ok"]),
            },
            "tag": {"type": "string", "maxLength": 8, "enum": ["pass", "fail"]},
        },
        ["count", "items"],
    )


@pytest.mark.parametrize("message", [None, "done"])
def test_nested_nullable_contract_preserves_original_json(message):
    schema = rich_schema()
    value = {"count": 4, "message": message, "items": [{"ok": True}], "tag": "pass"}
    original = copy.deepcopy(value)
    contracts.validate_public_output(schema, value)
    assert value == original
    captured = contracts.capture_public_output_schema(schema)
    schema["properties"].clear()
    assert captured["properties"]


@pytest.mark.parametrize(
    "value",
    [
        {"count": True, "items": []},
        {"count": 1.0, "items": []},
        {"count": -1, "items": []},
        {"count": 101, "items": []},
        {"count": 2, "items": [{"ok": 1}]},
        {"count": 2, "items": [{"ok": True, "debug": "private"}]},
        {"count": 2},
        {"count": 2, "items": [], "debug": "private"},
        {"count": 2, "items": [], "message": "x" * 65},
        {"count": 2, "items": [{"ok": True}] * 5},
        {"count": 2, "items": [], "tag": "other"},
    ],
)
def test_invalid_body_is_not_coerced_or_filtered(value):
    before = copy.deepcopy(value)
    with pytest.raises(ValueError):
        contracts.validate_public_output(rich_schema(), value)
    assert value == before


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object"},
        {"type": "object", "properties": {}, "additionalProperties": True},
        object_schema({"artifact": {"type": "null"}}),
        object_schema({}, ["missing"]),
        object_schema({"x": {"type": "boolean"}}, ["x", "x"]),
        object_schema({"x": {"type": "string"}}),
        object_schema({"x": {"type": "string", "maxLength": True}}),
        object_schema({"x": {"type": "string", "maxLength": 4097}}),
        object_schema({"x": {"type": "string", "maxLength": 1, "pattern": ".*"}}),
        object_schema({"x": {"$ref": "#"}}),
        object_schema({"x": {"$ref": "https://example.invalid/schema"}}),
        object_schema({"x": {"type": "array", "maxItems": 257, "items": {"type": "null"}}}),
        object_schema({"x": {"type": "array", "items": {"type": "null"}}}),
        object_schema({"x": {"type": "integer", "minimum": True}}),
        object_schema({"x": {"type": "integer", "minimum": 2, "maximum": 1}}),
        object_schema({"x": {"anyOf": []}}),
        object_schema({"x": {"anyOf": [{"type": "null"}] * 9}}),
        object_schema({"x": {"type": "string", "maxLength": 4, "enum": []}}),
        object_schema({"x": {"type": "null", "const": {}}}),
        object_schema({"x": {"type": "boolean", "allOf": []}}),
        object_schema({"x": {"anyOf": [{"type": "null"}], "title": False}}),
    ],
)
def test_unsafe_schema_is_rejected_before_descriptor_registration(schema):
    with pytest.raises(ValueError):
        contracts.capture_public_output_schema(schema)
    values = descriptor().model_dump(exclude={"public_output_schema"})
    with pytest.raises(ValidationError):
        ToolDescriptor(**values, public_output_schema=schema)


@pytest.mark.parametrize("case", ["cycle", "bytes", "nodes", "integer", "nan", "hook"])
def test_capture_enforces_native_budget_before_serialization(case):
    if case == "cycle":
        schema = object_schema({})
        schema["properties"]["self"] = schema
    elif case == "bytes":
        schema = object_schema(
            {
                str(n): {"type": "string", "maxLength": 1, "description": "x" * 4096}
                for n in range(10)
            }
        )
    elif case == "nodes":
        schema = object_schema(
            {
                str(n): {"type": "string", "maxLength": 1, "minLength": 0, "enum": ["x", "y"]}
                for n in range(100)
            }
        )
    elif case == "integer":
        schema = object_schema({"x": {"type": "integer", "minimum": 1 << 128}})
    elif case == "nan":
        schema = object_schema({"x": {"type": "number", "minimum": float("nan")}})
    else:

        class Dangerous(dict):
            def items(self):
                raise AssertionError("不应调用非原生对象")

        schema = Dangerous(type="object", properties={}, additionalProperties=False)
    with pytest.raises(ValueError):
        contracts.capture_public_output_schema(schema)


def test_anyof_uses_one_work_budget_and_propagates_checkpoint(monkeypatch):
    schema = object_schema({"x": {"anyOf": [{"type": "integer", "const": n} for n in range(8)]}})
    monkeypatch.setattr(contracts, "MAX_VALIDATION_WORK", 4)
    with pytest.raises(ValueError, match="工作上限"):
        contracts.validate_public_output(schema, {"x": 7})
    seen = []

    class Stop(Exception):
        pass

    def stop():
        seen.append(True)
        raise Stop()

    with pytest.raises(Stop):
        contracts.validate_public_output(schema, {"x": 7}, checkpoint=stop)
    assert seen == [True]


@pytest.mark.parametrize("value", [False, True, 1.0, None])
def test_const_integer_does_not_accept_boolean_or_float(value):
    schema = object_schema({"x": {"type": "integer", "const": 1}})
    with pytest.raises(ValueError):
        contracts.validate_public_output(schema, {"x": value})
