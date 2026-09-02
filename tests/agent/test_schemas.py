import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.models import AgentEvent, EventDraft, Thread
from harnessix.models.contracts import ProviderEvent


def test_generated_schemas_match_code() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "agent-event-v3.schema.json": AgentEvent.model_json_schema(),
        "agent-thread-v3.schema.json": Thread.model_json_schema(),
        "provider-event-v1.schema.json": TypeAdapter(ProviderEvent).json_schema(),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text()) == schema


def test_event_version_and_unknown_fields_fail_closed() -> None:
    with pytest.raises(ValidationError):
        EventDraft.model_validate(
            {
                "schema_version": 4,
                "payload": {"type": "thread_created", "workspace": "/tmp"},
            }
        )
    with pytest.raises(ValidationError):
        EventDraft.model_validate({"payload": {"type": "unknown_event", "workspace": "/tmp"}})
    with pytest.raises(ValidationError):
        EventDraft.model_validate(
            {"payload": {"type": "thread_created", "workspace": "/tmp", "secret": "canary"}}
        )


def test_approval_features_require_v2() -> None:
    from harnessix.agent.ids import new_id
    from harnessix.agent.models import (
        ApprovalRequestContent,
        ItemStarted,
        TurnStateChanged,
        TurnStatus,
    )

    for payload in [
        TurnStateChanged(status=TurnStatus.WAITING_APPROVAL),
        ItemStarted(
            item_id=new_id(),
            content=ApprovalRequestContent(
                approval_id=new_id(),
                call_id=new_id(),
                request_fingerprint="0" * 64,
            ),
        ),
    ]:
        with pytest.raises(ValidationError):
            EventDraft(schema_version=1, payload=payload)
        assert EventDraft(payload=payload).schema_version == 3
