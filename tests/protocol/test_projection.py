from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from harnessix.agent.models import (
    ApprovalRequestContent,
    Item,
    ItemStatus,
    ToolCallContent,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass
from harnessix.models.scripted import FakeProvider
from harnessix.protocol.projection import (
    project_event,
    project_item,
    project_replay,
    project_thread,
)
from harnessix.session.sqlite import SQLiteSessionStore


async def test_runtime_events_project_to_public_cursor_stream(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, FakeProvider("完成")) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "修复问题", request_id="turn-1")

    snapshot = await store.get_thread(thread.thread_id)
    internal = await store.events(thread.thread_id)
    public = tuple(
        projected
        for event in internal
        if (projected := project_event(thread.thread_id, event)) is not None
    )
    replay = project_replay(
        thread.thread_id,
        internal,
        scanned_through=internal[-1].sequence,
        has_more=False,
    )

    assert replay.scanned_through == snapshot.sequence
    assert replay.events == public
    assert tuple(event.cursor for event in public) == tuple(
        sorted({event.cursor for event in public})
    )
    assert len(public) < len(internal)
    assert any(
        right.cursor - left.cursor > 1 for left, right in zip(public, public[1:], strict=False)
    )
    assert {event.data.type for event in public} >= {
        "thread_created",
        "turn_started",
        "turn_state_changed",
        "item_started",
        "item_finished",
    }

    view = project_thread(snapshot)
    assert view.cursor == snapshot.sequence
    assert view.turn_count == 1
    assert view.latest_turn is not None
    assert view.latest_turn.status == "completed"

    encoded = json.dumps(
        [event.model_dump(mode="json", by_alias=True) for event in public],
        ensure_ascii=False,
    )
    assert "model_attempt" not in encoded
    assert "context_inspection" not in encoded
    assert "provider_call_id" not in encoded


def test_item_projection_removes_provider_and_private_approval_fields() -> None:
    call_id = uuid4()
    call = ToolCallContent(
        call_id=call_id,
        provider_call_id="provider-private-id",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
        arguments={"path": "README.md"},
        requires_approval=True,
        tool_fingerprint="a" * 64,
    )
    projected_call = project_item(Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=call))
    call_wire = projected_call.model_dump(mode="json", by_alias=True)["content"]
    assert call_wire["arguments"] == {"path": "README.md"}
    assert "providerCallId" not in call_wire
    assert "toolFingerprint" not in call_wire

    approval = ApprovalRequestContent(
        approval_id=uuid4(),
        call_id=call_id,
        request_fingerprint="b" * 64,
    )
    projected_approval = project_item(
        Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=approval)
    )
    approval_wire = projected_approval.model_dump(mode="json", by_alias=True)["content"]
    assert approval_wire["approvalType"] == "tool"
    assert approval_wire["requestFingerprint"] == "b" * 64
    assert "plan" not in approval_wire
