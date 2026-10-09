"""临时 Schema 标题节流不改变验证、字面数据、原目录或执行身份。"""

from copy import deepcopy
from typing import cast

import pytest
from jsonschema import Draft7Validator, Draft202012Validator
from pydantic import JsonValue

from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models._tool_schema import provider_tool_schema
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from tests.contracts.provider import model_request


@pytest.mark.parametrize(
    "keyword",
    ["$defs", "definitions", "properties", "patternProperties", "dependentSchemas", "dependencies"],
)
def test_schema_maps_keep_property_names_and_array_dependencies(keyword: str) -> None:
    schema = {
        "type": "object",
        "title": "Root",
        keyword: {"title": {"title": "Field", "type": "string"}},
    }
    original = deepcopy(schema)
    projected = provider_tool_schema(cast(dict[str, JsonValue], schema))
    assert projected == {"type": "object", keyword: {"title": {"type": "string"}}}
    assert schema == original
    assert provider_tool_schema(projected) == projected
    if keyword == "dependencies":
        assert provider_tool_schema({keyword: {"title": ["title", "value"]}}) == {
            keyword: {"title": ["title", "value"]}
        }


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf", "prefixItems"])
def test_schema_arrays_keep_boolean_schemas(keyword: str) -> None:
    assert provider_tool_schema({keyword: [{"title": "Field", "type": "string"}, False]}) == {
        keyword: [{"type": "string"}, False]
    }


@pytest.mark.parametrize(
    "keyword",
    [
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
    ],
)
def test_schema_children_only(keyword: str) -> None:
    assert provider_tool_schema({keyword: {"title": "Field", "type": "string"}}) == {
        keyword: {"type": "string"}
    }
    assert provider_tool_schema({keyword: False}) == {keyword: False}


def test_draft7_tuple_items() -> None:
    assert provider_tool_schema({"items": [{"title": "Field", "type": "string"}, True]}) == {
        "items": [{"type": "string"}, True]
    }


@pytest.mark.parametrize("keyword", ["const", "enum", "default", "examples", "x-extension"])
def test_literal_data_is_not_a_schema(keyword: str) -> None:
    value: JsonValue = {"title": "literal", "properties": {"title": "also literal"}}
    schema: dict[str, JsonValue] = {"title": "Root", keyword: value}
    if keyword in {"enum", "examples"}:
        schema[keyword] = [value]
    projected = provider_tool_schema(schema)
    assert projected == {keyword: schema[keyword]}
    # SDK 对临时对象的修改也不能反向改变注册合同。
    assert projected[keyword] is not schema[keyword]


@pytest.mark.parametrize("value", [None, False, 42, ["x"], {"title": "invalid"}])
def test_malformed_title_is_preserved(value: JsonValue) -> None:
    assert provider_tool_schema({"type": "object", "title": value}) == {
        "type": "object",
        "title": value,
    }


@pytest.mark.parametrize(
    "declaration",
    [
        {"$schema": "https://example.test/custom-schema"},
        {"$schema": {"title": "invalid"}},
        {"$vocabulary": {"https://example.test/custom-vocabulary": True}},
    ],
)
def test_unknown_dialects_and_vocabularies_are_not_rewritten(
    declaration: dict[str, JsonValue],
) -> None:
    schema = {**declaration, "title": "custom", "properties": {"title": {"title": "custom"}}}
    assert provider_tool_schema(schema) == schema
    assert provider_tool_schema({"properties": {"nested": schema}}) == {
        "properties": {"nested": schema}
    }


@pytest.mark.parametrize("validator", [Draft7Validator, Draft202012Validator])
@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "literal", "count": 2},
        {"title": "other", "count": 2},
        {"title": "literal", "count": 0},
        {"title": "literal", "count": "2"},
        {"title": "literal"},
        {"title": "literal", "count": 2, "extra": True},
    ],
)
def test_validation_constraints_are_identical(validator, arguments) -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "title": "Arguments",
        "additionalProperties": False,
        "required": ["title", "count"],
        "properties": {
            "title": {"title": "Literal", "const": "literal"},
            "count": {"title": "Count", "type": "integer", "minimum": 1, "maximum": 3},
        },
    }
    projected = provider_tool_schema(schema)
    validator.check_schema(schema)
    validator.check_schema(projected)
    assert validator(schema).is_valid(arguments) == validator(projected).is_valid(arguments)


@pytest.mark.parametrize("provider", ["chat", "anthropic"])
def test_provider_wire_only_not_original_descriptor(provider: str) -> None:
    request = model_request(with_tools=True)
    tool = request.tools[0].model_copy(
        update={
            "input_schema": {
                "type": "object",
                "title": "Arguments",
                "additionalProperties": False,
                "properties": {"title": {"type": "string", "title": "Title"}},
                "required": ["title"],
            }
        }
    )
    request = request.model_copy(update={"tools": (tool,)})
    original = request.model_dump_json()
    if provider == "chat":
        body, names = chat_request(request, OpenAIChatConfig(model="test"))
        projected = body["tools"][0]["function"]["parameters"]
    else:
        body, names = anthropic_request(request, AnthropicConfig(model="test"))
        projected = body["tools"][0]["input_schema"]
    assert projected == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    assert list(names.values()) == [tool.name]
    projected["properties"]["title"]["type"] = "number"
    assert request.model_dump_json() == original
