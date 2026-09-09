from __future__ import annotations

import json
from pathlib import Path

from harnessix.mcp.contracts import (
    McpCatalogSnapshot,
    McpConnectionEvent,
    McpConnectionSnapshot,
    McpServerIdentity,
    McpToolCallOutput,
    McpToolSnapshot,
)


def test_committed_mcp_schemas_match_runtime_contracts() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "mcp-server-identity-v1.schema.json": McpServerIdentity.model_json_schema(),
        "mcp-tool-snapshot-v1.schema.json": McpToolSnapshot.model_json_schema(),
        "mcp-catalog-snapshot-v1.schema.json": McpCatalogSnapshot.model_json_schema(),
        "mcp-connection-event-v1.schema.json": McpConnectionEvent.model_json_schema(),
        "mcp-connection-snapshot-v1.schema.json": (McpConnectionSnapshot.model_json_schema()),
        "mcp-tool-call-output-v1.schema.json": McpToolCallOutput.model_json_schema(),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text(encoding="utf-8")) == schema
