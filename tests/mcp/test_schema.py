from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.mcp.schema import (
    MAX_MCP_OUTPUT_BYTES,
    bounded_mcp_output,
    validate_mcp_arguments,
    validate_mcp_input_schema,
)


def schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 128},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def test_schema_and_arguments_use_bounded_json_schema_2020_12() -> None:
    checked = validate_mcp_input_schema(schema())
    parsed = validate_mcp_arguments(checked, {"query": "Harnessix", "limit": 3})

    assert parsed.root == {"query": "Harnessix", "limit": 3}

    with pytest.raises(KernelError, match="捕获Schema") as missing:
        validate_mcp_arguments(checked, {"limit": 3})
    assert missing.value.code == "tool_invalid_arguments"

    with pytest.raises(KernelError, match="捕获Schema"):
        validate_mcp_arguments(checked, {"query": "x", "unexpected": True})


def test_unresolvable_local_reference_is_reported_as_invalid_arguments() -> None:
    unresolved = {
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/missing"}},
    }

    with pytest.raises(KernelError) as caught:
        validate_mcp_arguments(unresolved, {"value": "x"})
    assert caught.value.code == "tool_invalid_arguments"


@pytest.mark.parametrize(
    "invalid",
    [
        {"type": "array"},
        {"$schema": "https://json-schema.org/draft/2019-09/schema", "type": "object"},
        {"type": "object", "properties": {"x": {"$ref": "https://evil.invalid/schema"}}},
        {"type": "object", "properties": {"x": {"type": "string", "pattern": "(a+)+$"}}},
    ],
)
def test_unbounded_or_external_schema_features_fail_closed(
    invalid: dict[str, object],
) -> None:
    with pytest.raises(KernelError) as caught:
        validate_mcp_input_schema(invalid)
    assert caught.value.code == "mcp_tool_schema_invalid"


def test_schema_and_output_bound_depth_and_bytes() -> None:
    nested: dict[str, object] = {"type": "object"}
    cursor = nested
    for index in range(40):
        child: dict[str, object] = {"type": "object"}
        cursor["properties"] = {f"x{index}": child}
        cursor = child

    with pytest.raises(KernelError, match="复杂度"):
        validate_mcp_input_schema(nested)

    with pytest.raises(KernelError) as oversized:
        bounded_mcp_output("x" * (MAX_MCP_OUTPUT_BYTES + 1))
    assert oversized.value.code == "mcp_result_too_large"
