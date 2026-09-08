import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harnessix.agent.models import AgentEvent, EventDraft, Thread
from harnessix.context import (
    ContextBuildInput,
    ContextConsistencySnapshot,
    ContextEngine,
    ContextFragment,
    ContextInspection,
    ContextInspectionV2,
    ContextInspectionV3,
    ContextLimits,
    ContextPrepared,
    ContextSourceDocument,
    ContextSourceObservation,
    ContextSourceSnapshot,
)
from harnessix.context.compaction_contracts import (
    CompactionAnchor,
    CompactionPlan,
    CompactionPolicy,
    CompactionSummary,
)
from harnessix.context.tool_result_contracts import (
    ModelHistoryInspection,
    ToolResultViewDecision,
    ToolResultViewPolicy,
)
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ProviderEvent
from harnessix.models.costs import CostReport
from harnessix.models.pricing import PriceSnapshot
from harnessix.smoke.contracts import SmokeConfig, SmokeReport


def test_generated_schemas_match_code() -> None:
    root = Path(__file__).parents[2] / "spec"
    expected = {
        "compaction-anchor-v1.schema.json": CompactionAnchor.model_json_schema(),
        "compaction-plan-v1.schema.json": CompactionPlan.model_json_schema(),
        "compaction-policy-v1.schema.json": CompactionPolicy.model_json_schema(),
        "compaction-summary-v1.schema.json": CompactionSummary.model_json_schema(),
        "model-history-inspection-v1.schema.json": ModelHistoryInspection.model_json_schema(),
        "tool-result-view-decision-v1.schema.json": ToolResultViewDecision.model_json_schema(),
        "tool-result-view-policy-v1.schema.json": ToolResultViewPolicy.model_json_schema(),
        "agent-event-v13.schema.json": AgentEvent.model_json_schema(),
        "agent-thread-v13.schema.json": Thread.model_json_schema(),
        "context-fragment-v1.schema.json": ContextFragment.model_json_schema(),
        "context-limits-v1.schema.json": ContextLimits.model_json_schema(),
        "context-inspection-v1.schema.json": ContextInspection.model_json_schema(),
        "context-inspection-v2.schema.json": ContextInspectionV2.model_json_schema(),
        "context-inspection-v3.schema.json": ContextInspectionV3.model_json_schema(),
        "context-consistency-v1.schema.json": ContextConsistencySnapshot.model_json_schema(),
        "context-source-document-v1.schema.json": ContextSourceDocument.model_json_schema(),
        "context-source-observation-v1.schema.json": ContextSourceObservation.model_json_schema(),
        "context-source-snapshot-v1.schema.json": ContextSourceSnapshot.model_json_schema(),
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
                "schema_version": 14,
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
        assert EventDraft(payload=payload).schema_version == 13


def test_context_inspection_requires_v10() -> None:
    inspection = (
        ContextEngine(ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024))
        .prepare(
            ContextBuildInput(
                thread_id=uuid4(),
                turn_id=uuid4(),
                model_step=1,
                workspace="/tmp",
            )
        )
        .inspection
    )
    with pytest.raises(ValidationError):
        EventDraft(schema_version=9, payload=ContextPrepared(inspection=inspection))
    assert EventDraft(schema_version=10, payload=ContextPrepared(inspection=inspection))
    assert EventDraft(payload=ContextPrepared(inspection=inspection)).schema_version == 13


def test_context_source_snapshot_requires_v11() -> None:
    legacy = (
        ContextEngine(ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024))
        .prepare(
            ContextBuildInput(
                thread_id=uuid4(),
                turn_id=uuid4(),
                model_step=1,
                workspace="/tmp",
            )
        )
        .inspection
    )
    current = ContextInspectionV2(
        **legacy.model_dump(exclude={"spec_version"}),
        sources=(
            ContextSourceSnapshot(
                source_id="project/instructions",
                kind="project_instruction",
                status="empty",
                workspace_scope="0" * 64,
                source_revision="1" * 64,
            ),
        ),
    )
    with pytest.raises(ValidationError):
        EventDraft(schema_version=10, payload=ContextPrepared(inspection=current))
    assert EventDraft(payload=ContextPrepared(inspection=current)).schema_version == 13


def test_context_consistency_snapshot_requires_v12() -> None:
    legacy = (
        ContextEngine(ContextLimits(context_window_tokens=4096, reserved_output_tokens=1024))
        .prepare(
            ContextBuildInput(
                thread_id=uuid4(),
                turn_id=uuid4(),
                model_step=1,
                workspace="/tmp",
            )
        )
        .inspection
    )
    sources = tuple(
        ContextSourceSnapshot(
            source_id=f"environment/source-{index}",
            kind="environment",
            status="empty",
            workspace_scope="0" * 64,
            source_revision=str(index) * 64,
        )
        for index in (1, 2)
    )
    current = ContextInspectionV3(
        **legacy.model_dump(exclude={"spec_version"}),
        sources=sources,
        consistency=ContextConsistencySnapshot(
            source_count=2,
            workspace_scope="0" * 64,
        ),
    )
    with pytest.raises(ValidationError):
        EventDraft(schema_version=11, payload=ContextPrepared(inspection=current))
    assert EventDraft(schema_version=12, payload=ContextPrepared(inspection=current))


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
            "agent-event-v10.schema.json": (
                "6f2d2c5c85b3af1ce6f2fe2fe52b71927417063f467c2c56ef9c5b4ad73310ea"
            ),
            "agent-thread-v10.schema.json": (
                "6fd70473fce49d6ad0f9190c90d674543d908eb3f0463adc97697e8b022dbbc5"
            ),
            "context-fragment-v1.schema.json": (
                "7be23065910b021658379b6273e96bf8c31fca9ac630dc49745823ba42a7e48c"
            ),
            "context-limits-v1.schema.json": (
                "e5723af0c49ce8673477e074602e1018edbe8a78f7b584f2855df7d8808758da"
            ),
            "context-inspection-v1.schema.json": (
                "a89b5c2f8b99b0975c7339e08ea0d43dfeedfb218de7304cf3f8874f281fdf97"
            ),
            "agent-event-v11.schema.json": (
                "d849fde63ef0d2e8d28e4302cfd744e4be09167d197a6a7530ff1fb666cb117e"
            ),
            "agent-thread-v11.schema.json": (
                "03d7ff1a8044c40973865fac6e684061f64f1e19aef734bcb2b31203d5ffbd8a"
            ),
            "context-inspection-v2.schema.json": (
                "d45ebeca167d458d0a932fe5512f10886d4dd1e1ededc803ad3337114c72dac1"
            ),
            "agent-event-v9.schema.json": (
                "48256f8dd49f9feaaf8125f96febac334a767903eb426307588d83933d4aedea"
            ),
            "agent-thread-v9.schema.json": (
                "1f3723e328083aee127a1b28af198e135647f086b5be60af2dbe3e35dbeaf103"
            ),
            "agent-event-v8.schema.json": (
                "d83381b4dffa5854ad4c5997a775e617800c3304481c88f10e3b7b9021a23fa3"
            ),
            "agent-thread-v8.schema.json": (
                "5874c0d4eef02d0cc473ed12bbf5cf7f529eff6508087c9f7ac1a2a7f57f4608"
            ),
            "agent-event-v7.schema.json": (
                "f58693e351a8d424056d12668faffb693c27233c8a77e04b40025616e02a17ce"
            ),
            "agent-thread-v7.schema.json": (
                "18301bc8a97b6c7fb2552d079554e8f989963fc89b4ca42b8eb7f61954a8c4aa"
            ),
        }
    )
    expected.update(
        {
            "agent-event-v6.schema.json": (
                "5e4151227b4d47f952953058b358d673ff6721a8fd298d0a91c578771f78ab50"
            ),
            "agent-thread-v6.schema.json": (
                "a480fac71fa5f1542536c0b6507fd320016893e353047d6fd2bd8bc0246d83a1"
            ),
        }
    )
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
    expected.update(
        {
            "agent-event-v12.schema.json": (
                "11d1ebecece86e2cc279dffd6b6adfa3f5154bfb537e26b86ec1b150146effa7"
            ),
            "agent-thread-v12.schema.json": (
                "bb0a7d079bd9e04de337cdcb0e3c5609205cc470328c7c3cc2f3ee33fc808d5d"
            ),
        }
    )
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
