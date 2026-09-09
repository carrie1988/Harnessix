from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.protocol.compatibility import decode_known_notification
from harnessix.protocol.contracts import (
    ClientInfo,
    EventsNextResult,
    EventsReplayResult,
    InitializeParams,
    JsonRpcError,
    JsonRpcErrorData,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcSuccessResponse,
    ProtocolLimits,
    PublicBudget,
    PublicEvent,
    PublicItemDelta,
    PublicUsage,
    ServerCapabilities,
    ServerInfo,
    ThreadCreateParams,
    ThreadPublicEvent,
    ThreadView,
    TurnStartParams,
    validate_server_output,
)


def _thread_view() -> ThreadView:
    now = datetime.now(UTC)
    return ThreadView(
        thread_id=uuid4(),
        workspace="/tmp/workspace",
        cursor=0,
        active_turn_id=None,
        turn_count=0,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("request_id", [None, True, 1.5, "", "x" * 129, 1 << 53])
def test_jsonrpc_request_rejects_ambiguous_or_unbounded_ids(request_id: object) -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate(
            {"jsonrpc": "2.0", "id": request_id, "method": "thread/get", "params": {}}
        )


def test_jsonrpc_wire_shape_is_standard_and_strict() -> None:
    request = JsonRpcRequest(id="rpc-1", method="thread/get", params={"threadId": "id"})
    assert request.model_dump(mode="json", by_alias=True) == {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "thread/get",
        "params": {"threadId": "id"},
    }
    success = JsonRpcSuccessResponse(id="rpc-1", result={"ok": True})
    error = JsonRpcErrorResponse(
        id="rpc-1",
        error=JsonRpcError(
            code=-32602,
            message="Invalid params",
            data=JsonRpcErrorData(code="invalid_params", path=("threadId",)),
        ),
    )
    assert "error" not in success.model_dump(mode="json", by_alias=True)
    assert "result" not in error.model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError):
        JsonRpcSuccessResponse.model_validate(
            {"jsonrpc": "2.0", "id": "rpc-1", "result": {}, "error": {}}
        )


def test_initialize_and_command_params_use_camel_case_and_reject_unknown_fields() -> None:
    initialized = InitializeParams(
        protocol_version="1.0",
        client_info=ClientInfo(name="harnessix-cli", version="0.8.0"),
        client_instance_id=uuid4(),
    )
    wire = initialized.model_dump(mode="json", by_alias=True)
    assert wire["protocolVersion"] == "1.0"
    assert wire["clientInfo"]["name"] == "harnessix-cli"
    assert "client_instance_id" not in wire

    command = ThreadCreateParams(request_id="create-1", workspace="/tmp/workspace")
    assert command.model_dump(mode="json", by_alias=True)["requestId"] == "create-1"
    with pytest.raises(ValidationError):
        TurnStartParams.model_validate(
            {
                "requestId": "turn-1",
                "threadId": str(uuid4()),
                "prompt": "修复问题",
                "unknownBudget": 1,
            }
        )
    with pytest.raises(ValidationError):
        InitializeParams.model_validate({**wire, "futureCapability": True})


def test_capabilities_must_be_stably_sorted_and_unique() -> None:
    with pytest.raises(ValidationError):
        ServerCapabilities(methods=("thread/list", "thread/get"))
    with pytest.raises(ValidationError):
        ServerCapabilities(methods=("thread/get", "thread/get"))
    capabilities = ServerCapabilities(
        methods=("thread/get", "thread/list"),
        notifications=("thread/updated",),
    )
    assert capabilities.methods == ("thread/get", "thread/list")


def test_old_client_ignores_unknown_outputs_but_known_fields_remain_strict() -> None:
    original = _thread_view()
    wire = original.model_dump(mode="json", by_alias=True)
    wire["futureOptionalField"] = {"enabled": True}
    decoded = validate_server_output(ThreadView, wire)
    assert decoded.thread_id == original.thread_id

    invalid = {**wire, "cursor": "not-an-integer"}
    with pytest.raises(ValidationError):
        validate_server_output(ThreadView, invalid)


def test_unknown_notification_is_ignored_after_envelope_validation() -> None:
    assert (
        decode_known_notification(
            {"jsonrpc": "2.0", "method": "future/notification", "params": {"value": 1}}
        )
        is None
    )
    known = decode_known_notification(
        {"jsonrpc": "2.0", "method": "thread/updated", "params": {"cursor": 2}}
    )
    assert known is not None and known.method == "thread/updated"
    with pytest.raises(ValidationError):
        decode_known_notification(
            {"jsonrpc": "2.0", "method": "thread/updated", "params": {}, "id": 1}
        )


def test_replay_accepts_cursor_gaps_but_rejects_reordering() -> None:
    thread_id = uuid4()
    now = datetime.now(UTC)

    def event(cursor: int) -> PublicEvent:
        return PublicEvent(
            event_id=uuid4(),
            thread_id=thread_id,
            cursor=cursor,
            occurred_at=now,
            data=ThreadPublicEvent(type="thread_created", workspace="/tmp/workspace"),
        )

    replay = EventsReplayResult(
        thread_id=thread_id,
        events=(event(1), event(7)),
        scanned_through=9,
        has_more=False,
    )
    assert [item.cursor for item in replay.events] == [1, 7]
    with pytest.raises(ValidationError):
        EventsReplayResult(
            thread_id=thread_id,
            events=(event(7), event(1)),
            scanned_through=9,
            has_more=False,
        )


def test_next_events_rejects_cross_thread_or_duplicate_deltas() -> None:
    thread_id = uuid4()
    replay = EventsReplayResult(
        thread_id=thread_id,
        events=(),
        scanned_through=0,
        has_more=False,
    )
    delta = PublicItemDelta(
        thread_id=thread_id,
        turn_id=uuid4(),
        item_id=uuid4(),
        model_step=1,
        stream_sequence=1,
        delta="增量",
    )
    assert EventsNextResult(replay=replay, deltas=(delta,)).deltas == (delta,)
    with pytest.raises(ValidationError):
        EventsNextResult(
            replay=replay,
            deltas=(delta.model_copy(update={"thread_id": uuid4()}),),
        )
    with pytest.raises(ValidationError):
        EventsNextResult(replay=replay, deltas=(delta, delta))
    with pytest.raises(ValidationError):
        EventsNextResult(replay=replay, deltas=(delta,), timed_out=True)
    with pytest.raises(ValidationError):
        EventsNextResult(replay=replay, live_gap=True, timed_out=True)


def test_public_budget_and_limits_are_finite_and_bounded() -> None:
    with pytest.raises(ValidationError):
        PublicBudget(
            max_steps=1,
            max_tokens=1,
            timeout_seconds=float("inf"),
            max_output_chars=1,
            max_tool_calls_per_step=1,
        )
    with pytest.raises(ValidationError):
        ProtocolLimits(max_message_bytes=1024)
    with pytest.raises(ValidationError):
        PublicUsage(input_tokens=1, output_tokens=2, total_tokens=4)


def test_server_info_has_stable_product_identity() -> None:
    assert ServerInfo(version="0.8.0").model_dump(mode="json", by_alias=True) == {
        "name": "harnessix-code",
        "version": "0.8.0",
    }
