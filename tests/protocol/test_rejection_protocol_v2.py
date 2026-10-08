"""公共拒绝事实的 V2 合同、脱敏投影与正常 Item 回归。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.models import (
    AgentEvent,
    ApprovalRequestContent,
    Item,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
)
from harnessix.domain.models import EffectClass
from harnessix.protocol.contracts import (
    AGENT_PROTOCOL_VERSION,
    EventsReplayResult,
    PublicEvent,
    PublicItem,
    PublicItemContent,
    PublicToolCallContent,
    PublicToolCallRejectionContent,
)
from harnessix.protocol.projection import project_event, project_item, project_replay


def rejection_wire() -> dict[str, object]:
    return {
        "kind": "tool_call_rejection",
        "callId": str(uuid4()),
        "modelStep": 1,
        "reason": "unregistered_tool",
    }


def test_v2_schema_exposes_only_the_four_public_rejection_fields() -> None:
    assert AGENT_PROTOCOL_VERSION == "2.0"
    schema = PublicToolCallRejectionContent.model_json_schema(by_alias=True)
    assert set(schema["properties"]) == {"kind", "callId", "modelStep", "reason"}
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"callId", "modelStep"}
    assert schema["properties"]["kind"]["const"] == "tool_call_rejection"
    assert schema["properties"]["callId"]["format"] == "uuid"
    assert schema["properties"]["modelStep"]["type"] == "integer"
    assert schema["properties"]["modelStep"]["minimum"] == 1
    assert schema["properties"]["modelStep"]["maximum"] == 1000
    assert schema["properties"]["reason"]["const"] == "unregistered_tool"
    event_schema = PublicEvent.model_json_schema(by_alias=True)
    assert event_schema["properties"]["specVersion"]["const"] == (
        "harnessix.agent-protocol-event/v2"
    )
    item_schema = event_schema["$defs"]["PublicItem"]["properties"]["content"]
    assert item_schema["discriminator"]["mapping"]["tool_call_rejection"] == (
        "#/$defs/PublicToolCallRejectionContent"
    )
    assert event_schema["$defs"]["PublicToolCallRejectionContent"] == schema


@pytest.mark.parametrize("model_step", [1, 1000])
def test_item_projection_whitelists_fields_and_removes_provider_secret(model_step: int) -> None:
    secret = "provider-secret-name-args-chars-Contract"
    content = ToolCallRejectionContent(
        call_id=uuid4(), provider_call_id=secret, model_step=model_step
    )
    internal = Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)
    public = project_item(internal)
    assert isinstance(public.content, PublicToolCallRejectionContent)
    assert not isinstance(public.content, PublicToolCallContent)
    wire = public.model_dump(mode="json", by_alias=True)
    assert wire["content"] == {
        "kind": "tool_call_rejection",
        "callId": str(content.call_id),
        "modelStep": model_step,
        "reason": "unregistered_tool",
    }
    assert wire["itemId"] == str(internal.item_id)
    assert wire["status"] == "completed"
    assert wire["error"] is None
    assert secret not in public.model_dump_json()
    assert PublicItem.model_validate_json(public.model_dump_json()) == public
    parsed = TypeAdapter(PublicItemContent).validate_json(json.dumps(wire["content"]))
    assert parsed == public.content


@pytest.mark.parametrize(
    "field",
    [
        "providerCallId",
        "provider_call_id",
        "name",
        "args",
        "chars",
        "Contract",
        "contract",
        "tool",
        "arguments",
        "requiresApproval",
        "toolVersion",
        "effectClass",
    ],
)
def test_public_rejection_rejects_private_or_executable_extra_fields(field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        PublicToolCallRejectionContent.model_validate_json(
            json.dumps({**rejection_wire(), field: "private-value"})
        )
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("model_step", [0, -1, 1001, True, 1.0, "1", None])
def test_public_rejection_requires_a_strict_bounded_model_step(model_step: object) -> None:
    with pytest.raises(ValidationError):
        PublicToolCallRejectionContent.model_validate_json(
            json.dumps({**rejection_wire(), "modelStep": model_step})
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("callId", "provider-id"), ("kind", "tool_call"), ("reason", "unknown_tool")],
)
def test_public_rejection_validates_uuid_and_fixed_literals(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        PublicToolCallRejectionContent.model_validate_json(
            json.dumps({**rejection_wire(), field: value})
        )


@pytest.mark.parametrize("finished", [False, True])
def test_internal_v21_events_project_to_public_v2_without_private_fields(finished: bool) -> None:
    thread_id, turn_id, item_id = uuid4(), uuid4(), uuid4()
    content = ToolCallRejectionContent(
        call_id=uuid4(), provider_call_id="private-provider-canary", model_step=2
    )
    payload = (
        ItemFinished(item_id=item_id, status=ItemStatus.COMPLETED, content=content)
        if finished
        else ItemStarted(item_id=item_id, content=content)
    )
    internal = AgentEvent(thread_id=thread_id, turn_id=turn_id, sequence=3, payload=payload)
    public = project_event(thread_id, internal)
    assert public is not None
    assert internal.schema_version == 21
    assert public.spec_version == "harnessix.agent-protocol-event/v2"
    assert public.event_id == internal.event_id
    assert public.turn_id == turn_id
    assert public.cursor == 3
    assert public.data.type == payload.type
    wire = public.model_dump(mode="json", by_alias=True)
    assert set(wire["data"]["item"]["content"]) == {"kind", "callId", "modelStep", "reason"}
    assert "schemaVersion" not in wire
    assert "private-provider-canary" not in public.model_dump_json()
    assert "provider_call_id" not in public.model_dump_json()
    assert "providerCallId" not in public.model_dump_json()
    assert PublicEvent.model_validate_json(public.model_dump_json()) == public
    with pytest.raises(ValidationError):
        PublicEvent.model_validate_json(
            json.dumps({**wire, "specVersion": "harnessix.agent-protocol-event/v1"})
        )
    replay = project_replay(thread_id, [internal], scanned_through=3, has_more=False)
    assert replay.events == (public,)
    assert EventsReplayResult.model_validate_json(replay.model_dump_json()) == replay
    assert "private-provider-canary" not in replay.model_dump_json()


def test_normal_items_retain_their_existing_public_shapes() -> None:
    call_id = uuid4()
    contents = (
        TextContent(kind="assistant_message", text="已完成"),
        ToolCallContent(
            call_id=call_id,
            provider_call_id="private-normal-provider",
            tool="workspace.read",
            tool_version="1",
            effect_class=EffectClass.READ_ONLY,
            arguments={"path": "README.md"},
            requires_approval=True,
        ),
        ToolResultContent(call_id=call_id, outcome="succeeded", output={"text": "正文"}),
        ApprovalRequestContent(call_id=call_id, approval_id=uuid4(), request_fingerprint="a" * 64),
    )
    wires = [
        project_item(
            Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)
        ).model_dump(mode="json", by_alias=True)["content"]
        for content in contents
    ]
    assert wires[0] == {"kind": "assistant_message", "text": "已完成"}
    assert wires[1] == {
        "kind": "tool_call",
        "callId": str(call_id),
        "tool": "workspace.read",
        "toolVersion": "1",
        "effectClass": "read_only",
        "arguments": {"path": "README.md"},
        "requiresApproval": True,
    }
    assert wires[2] == {
        "kind": "tool_result",
        "callId": str(call_id),
        "outcome": "succeeded",
        "output": {"text": "正文"},
        "error": None,
        "actionId": None,
        "diffArtifact": None,
    }
    assert wires[3]["approvalType"] == "tool"
    assert wires[3]["requestFingerprint"] == "a" * 64
    assert "private-normal-provider" not in json.dumps(wires)
