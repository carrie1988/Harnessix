import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.models import AgentEvent, EventDraft, Thread
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent
from harnessix.models.costs import CostReport
from harnessix.models.pricing import PriceSnapshot
from harnessix.smoke.contracts import SmokeConfig, SmokeReport


def test_generated_schemas_match_code() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "agent-event-v6.schema.json": AgentEvent.model_json_schema(),
        "agent-thread-v6.schema.json": Thread.model_json_schema(),
        "provider-event-v3.schema.json": TypeAdapter(ProviderEvent).json_schema(),
        "openai-chat-config-v1.schema.json": OpenAIChatConfig.model_json_schema(),
        "anthropic-config-v1.schema.json": AnthropicConfig.model_json_schema(),
        "price-snapshot-v1.schema.json": PriceSnapshot.model_json_schema(),
        "cost-report-v1.schema.json": CostReport.model_json_schema(),
        "model-smoke-config-v1.schema.json": SmokeConfig.model_json_schema(),
        "model-smoke-report-v1.schema.json": SmokeReport.model_json_schema(),
    }
    for name, schema in expected.items():
        assert json.loads((root / name).read_text()) == schema


def test_event_version_and_unknown_fields_fail_closed() -> None:
    with pytest.raises(ValidationError):
        EventDraft.model_validate(
            {
                "schema_version": 7,
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
        assert EventDraft(payload=payload).schema_version == 6


def test_historical_schemas_are_frozen() -> None:
    import hashlib

    root = Path(__file__).parents[2] / "spec"
    expected = {
        "agent-event-v1.schema.json": (
            "0ebace25ba3e013d701a4a0b870de244fc481ebac7fc733b853377a11caea5a5"
        ),
        "agent-event-v2.schema.json": (
            "79c64291b4d36031f4a4a6571bd7cf393f0499ac5113bddc5fd77e23755037e5"
        ),
        "agent-event-v3.schema.json": (
            "5b4e8092db3166a1e668f4eea447e089eba55080f947b0edefe00792a4a55e04"
        ),
        "agent-thread-v1.schema.json": (
            "a326312cbd4771a16fd67ee1324a20a21060ac9d7de15688e6cfc86539b2f6c6"
        ),
        "agent-thread-v2.schema.json": (
            "d26721ca6a15b4139461c2175d377d9819bd3c784632e6939a6e00ef0aecdbdc"
        ),
        "agent-thread-v3.schema.json": (
            "e1f4313ec46b05e9535e11fdf89187673cf14be16d8793f10b6f3608b918dec7"
        ),
        "provider-event-v1.schema.json": (
            "50f9652a58b75137240b8fd8d955d077947cd02a989b79dc94939c1b09905537"
        ),
    }
    expected.update(
        {
            "agent-event-v5.schema.json": (
                "d4eab9ea7bf8c0fdb6521e5c95a567ecf4ff032ad6b6b432660d6f558b270c57"
            ),
            "agent-thread-v5.schema.json": (
                "cbc21a0a72b64b029702eba1fa1eb70ebbcd6aa9819a843b1b1b99bb82afbd2c"
            ),
            "agent-event-v4.schema.json": (
                "132e0cfe50e55639ac1ef5facfaff44525e404d09ff0c2c800a6a49aeee25b81"
            ),
            "agent-thread-v4.schema.json": (
                "51758f5c23dac2295bc8ca80d4a2ffad916227c02738d0986fc4bbc32974a60f"
            ),
            "provider-event-v2.schema.json": (
                "278d5032650328c24e0939011864e8928cddce5c0106b81bae1554bc9c9f0eb5"
            ),
        }
    )
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
