"""V2 初始化协商与不支持版本、坏参数的稳定错误语义。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.app_server.handshake import PreparedInitialization, prepare_initialization
from harnessix.protocol.contracts import (
    AGENT_PROTOCOL_VERSION,
    InitializeParams,
    InitializeResult,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    ProtocolLimits,
    validate_protocol_input,
)
from harnessix.sdk.agent_client import AgentClient


def initialize_request(version: object = "2.0") -> JsonRpcRequest:
    return JsonRpcRequest(
        id="initialize-v2",
        method="initialize",
        params={
            "protocolVersion": version,
            "clientInfo": {"name": "test-v2", "version": "1"},
            "clientInstanceId": str(uuid4()),
            "capabilities": {"itemDeltas": True},
        },
    )


def prepare(request: JsonRpcRequest, *, is_new: bool = True) -> bytes | PreparedInitialization:
    return prepare_initialization(
        request,
        is_new=is_new,
        limits=ProtocolLimits(),
        methods=("thread/get",),
        artifact_pages=True,
        version="test-server",
    )


def assert_error(response: bytes | PreparedInitialization, code: str) -> JsonRpcErrorResponse:
    assert isinstance(response, bytes)
    error = JsonRpcErrorResponse.model_validate_json(response)
    assert error.id == "initialize-v2"
    assert error.error.code == -32602
    assert error.error.data.code == code
    assert error.error.data.retryable is False
    assert error.error.data.path == ("protocolVersion",)
    return error


def test_initialize_v2_preserves_identity_capabilities_and_limit_negotiation() -> None:
    request = initialize_request()
    request.params["limits"] = {
        "maxMessageBytes": 4096,
        "maxPendingRequests": 1,
        "maxOutboundMessages": 8,
        "maxReplayEvents": 1,
    }
    response = prepare(request)
    assert isinstance(response, PreparedInitialization)
    wire = json.loads(response.frame)
    assert wire["id"] == request.id
    result = InitializeResult.model_validate_json(json.dumps(wire["result"]))
    assert result.protocol_version == AGENT_PROTOCOL_VERSION == "2.0"
    assert (
        response.client_instance_id
        == validate_protocol_input(InitializeParams, request.params).client_instance_id
    )
    assert response.item_deltas_enabled is True
    assert result.capabilities.item_deltas is True
    assert result.capabilities.methods == ("thread/get",)
    assert response.limits == result.limits
    assert response.limits.max_message_bytes == 4096
    assert response.limits.max_pending_requests == 1
    assert response.limits.max_outbound_messages == 8
    assert response.limits.max_replay_events == 1


@pytest.mark.parametrize("version", ["1.0", "3.0", "invalid", "2", "2.0 ", "x" * 32])
def test_well_formed_versions_reach_explicit_handshake_rejection(version: str) -> None:
    request = initialize_request(version)
    assert validate_protocol_input(InitializeParams, request.params).protocol_version == version
    response = prepare(request)
    error = assert_error(response, "unsupported_protocol_version")
    assert error.error.message == "不支持的Agent Protocol版本"
    assert "result" not in json.loads(response)
    assert version not in error.error.message


@pytest.mark.parametrize("version", ["", "x" * 33, 2, 2.0, True, None, [], {}])
def test_malformed_versions_remain_invalid_params_without_echoing_input(version: object) -> None:
    request = initialize_request(version)
    with pytest.raises(ValidationError):
        validate_protocol_input(InitializeParams, request.params)
    error = assert_error(prepare(request), "invalid_params")
    assert error.error.message == "initialize参数无效"


def test_missing_version_is_invalid_params() -> None:
    request = initialize_request()
    del request.params["protocolVersion"]
    assert_error(prepare(request), "invalid_params")


def test_rejected_v1_candidate_does_not_prevent_fresh_v2_candidate() -> None:
    assert_error(prepare(initialize_request("1.0")), "unsupported_protocol_version")
    assert isinstance(prepare(initialize_request("2.0")), PreparedInitialization)


def test_reinitialization_still_has_priority_over_version_validation() -> None:
    response = prepare(initialize_request("1.0"), is_new=False)
    assert isinstance(response, bytes)
    error = JsonRpcErrorResponse.model_validate_json(response)
    assert error.error.code == -32600
    assert error.error.data.code == "already_initialized"


def test_initialize_schema_separates_bounded_input_from_fixed_v2_output() -> None:
    version = InitializeParams.model_json_schema()["properties"]["protocolVersion"]
    assert version["type"] == "string"
    assert version["minLength"] == 1
    assert version["maxLength"] == 32
    assert "const" not in version
    result_schema = InitializeResult.model_json_schema()["properties"]["protocolVersion"]
    assert result_schema["const"] == result_schema["default"] == "2.0"
    response = prepare(initialize_request())
    assert isinstance(response, PreparedInitialization)
    wire = json.loads(response.frame)["result"]
    with pytest.raises(ValidationError):
        InitializeResult.model_validate_json(json.dumps({**wire, "protocolVersion": "1.0"}))


async def test_sdk_automatically_uses_the_shared_v2_constant_without_dual_stack() -> None:
    class HandshakeTransport:
        """仅在内存中交换握手帧，不创建服务或网络连接。"""

        def __init__(self) -> None:
            self.requests: list[JsonRpcRequest] = []
            self.notifications: list[bytes] = []
            self.closed = False

        async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
            request = JsonRpcRequest.model_validate_json(frame)
            self.requests.append(request)
            response = prepare(request)
            assert isinstance(response, PreparedInitialization)
            return (response.frame,)

        async def notify(self, frame: bytes) -> None:
            self.notifications.append(frame)

        async def close(self) -> None:
            self.closed = True

    transport = HandshakeTransport()
    client = AgentClient(transport)
    result = await client.initialize()
    assert result.protocol_version == "2.0"
    assert len(transport.requests) == 1
    assert transport.requests[0].params["protocolVersion"] == "2.0"
    assert json.loads(transport.notifications[0])["method"] == "notifications/initialized"
    assert await client.initialize() is result
    assert len(transport.requests) == 1
    await client.close()
    assert transport.closed
