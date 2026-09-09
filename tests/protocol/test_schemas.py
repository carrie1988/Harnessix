from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from harnessix.protocol.contracts import (
    AgentCommandParams,
    AgentQueryParams,
    EventsReplayResult,
    InitializeParams,
    InitializeResult,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    PublicEvent,
    PublicItem,
    ThreadView,
    TurnView,
)


def test_agent_protocol_schemas_match_runtime_contracts() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "agent-protocol-jsonrpc-request-v1.schema.json": JsonRpcRequest.model_json_schema(),
        "agent-protocol-jsonrpc-notification-v1.schema.json": (
            JsonRpcNotification.model_json_schema()
        ),
        "agent-protocol-jsonrpc-success-v1.schema.json": (
            JsonRpcSuccessResponse.model_json_schema()
        ),
        "agent-protocol-jsonrpc-error-v1.schema.json": (JsonRpcErrorResponse.model_json_schema()),
        "agent-protocol-initialize-params-v1.schema.json": InitializeParams.model_json_schema(),
        "agent-protocol-initialize-result-v1.schema.json": InitializeResult.model_json_schema(),
        "agent-protocol-thread-v1.schema.json": ThreadView.model_json_schema(),
        "agent-protocol-turn-v1.schema.json": TurnView.model_json_schema(),
        "agent-protocol-item-v1.schema.json": PublicItem.model_json_schema(),
        "agent-protocol-event-v1.schema.json": PublicEvent.model_json_schema(),
        "agent-protocol-replay-result-v1.schema.json": EventsReplayResult.model_json_schema(),
        "agent-protocol-command-params-v1.schema.json": (
            TypeAdapter(AgentCommandParams).json_schema()
        ),
        "agent-protocol-query-params-v1.schema.json": TypeAdapter(AgentQueryParams).json_schema(),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text(encoding="utf-8")) == schema
