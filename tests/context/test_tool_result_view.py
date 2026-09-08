from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    Budget,
    EventDraft,
    Item,
    ItemStatus,
    ModelHistoryPrepared,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import apply_event
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.context.tool_result_contracts import ToolResultViewDecision, ToolResultViewPolicy
from harnessix.context.tool_result_view import prepare_model_history
from harnessix.domain.models import EffectClass, utc_now


def sample(output, *, archived=False, complete=True):
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="call",
        tool="test.read",
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
    )
    if archived:
        body = (json.dumps(output, ensure_ascii=False) + "\n").encode()
        ref = ArtifactRef(
            artifact_id=uuid4(),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            records=1,
            complete=complete,
            expires_at=utc_now() + timedelta(hours=1),
        )
        output = {"preview": output, "artifact": ref.model_dump(mode="json")}
    result = ToolResultContent(call_id=call.call_id, outcome="succeeded", output=output)
    items = tuple(
        Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)
        for content in (TextContent(kind="user_message", text="检查"), call, result)
    )
    turn = Turn(
        turn_id=uuid4(),
        request_id="request",
        request_fingerprint="0" * 64,
        budget=Budget(),
        created_at=utc_now(),
        items=items,
        status=TurnStatus.PREPARING_CONTEXT,
    )
    return Thread(
        thread_id=uuid4(),
        workspace="/workspace",
        turns=(turn,),
        sequence=1,
        active_turn_id=turn.turn_id,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def prepared(thread, limit=2048, **kwargs):
    return prepare_model_history(
        thread, 1, ToolResultViewPolicy(max_inline_utf8_bytes=limit), **kwargs
    )


def with_turn(thread, **changes):
    return thread.model_copy(update={"turns": (thread.turns[0].model_copy(update=changes),)})


@pytest.mark.parametrize(
    "output", [None, True, 1, 1.25, [], {}, {"嵌套": [None, "🙂", {"a": "é"}]}]
)
def test_inline_preserves_json_types_and_unicode(output):
    thread = sample(output)
    before = thread.model_dump_json()
    result = prepared(thread)
    (decision,) = result.new_decisions
    assert decision.strategy == "inline"
    assert decision.source_sha256 == decision.view_sha256
    assert decision.source_utf8_bytes == decision.view_utf8_bytes
    assert result.history == thread.turns[0].items
    assert thread.model_dump_json() == before
    assert prepared(thread) == result
    assert prepared(thread, decisions=result.new_decisions) == result


def test_utf8_exact_limit_and_unsupported_large_result():
    thread = sample("汉🙂" * 250)
    size = prepared(thread, 4096).new_decisions[0].source_utf8_bytes
    assert size > 1024
    assert prepared(thread, size).new_decisions[0].view_utf8_bytes == size
    with pytest.raises(KernelError) as error:
        prepared(thread, size - 1)
    assert error.value.code == "context_tool_result_artifact_required"


def test_replacement_is_json_preserves_source_and_freezes_exact_view():
    thread = sample({"数据": ["needle🙂" * 1000]}, archived=True)
    before = thread.model_dump_json()
    result = prepared(thread)
    (decision,) = result.new_decisions
    assert decision.strategy == "artifact_reference"
    assert decision.view_utf8_bytes < decision.source_utf8_bytes
    output = result.history[-1].content.output
    assert output["preview"] is None
    assert output["model_view"]["omitted_field"] == "preview"
    assert result.references[0].omitted_field == "preview"
    assert thread.model_dump_json() == before
    frozen = with_turn(
        thread,
        tool_result_view_decisions=result.new_decisions,
        model_history_inspections=(result.inspection,),
    )
    reused = prepared(frozen, 80000)
    assert reused.history == result.history and not reused.new_decisions
    assert reused.inspection.decisions_sha256 == result.inspection.decisions_sha256
    output["model_view"]["reason"] = "mutated"
    assert (
        prepared(frozen).history[-1].content.output["model_view"]["reason"]
        == "inline_budget_exceeded"
    )
    assert thread.model_dump_json() == before


def test_inline_decision_cannot_be_replaced_by_smaller_policy():
    thread = sample("🙂" * 1000, archived=True)
    first = prepared(thread, 20000)
    frozen = with_turn(
        thread,
        tool_result_view_decisions=first.new_decisions,
        model_history_inspections=(first.inspection,),
    )
    with pytest.raises(KernelError) as error:
        prepared(frozen, 2048)
    assert error.value.code == "context_tool_result_decision_mismatch"


@pytest.mark.parametrize("mode", ["incomplete", "invalid", "extra", "legacy"])
def test_unsafe_replacements_are_rejected(mode):
    thread = sample("x" * 10000, archived=True, complete=mode != "incomplete")
    output = thread.turns[0].items[-1].content.output
    if mode == "invalid":
        output["artifact"]["size_bytes"] = "10000"
    elif mode == "extra":
        output["status"] = "must not disappear"
    elif mode == "legacy":
        thread = with_turn(thread, model_steps=1)
    with pytest.raises(KernelError) as error:
        prepared(thread)
    assert (
        error.value.code
        == {
            "incomplete": "context_tool_result_artifact_incomplete",
            "invalid": "context_tool_result_artifact_invalid",
            "extra": "context_tool_result_unsupported",
            "legacy": "context_tool_result_decision_mismatch",
        }[mode]
    )


def test_legacy_small_result_is_frozen_inline_and_not_retroactively_rewritten():
    thread = with_turn(sample("already seen", archived=True), model_steps=1)
    result = prepared(thread)
    assert result.new_decisions[0].strategy == "inline"
    assert prepared(thread, decisions=result.new_decisions) == result


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha256", "1" * 64),
        ("view_sha256", "2" * 64),
        ("item_id", uuid4()),
        ("call_id", uuid4()),
        ("limit_utf8_bytes", 4096),
    ],
)
def test_replayed_decision_is_bound_to_source_and_first_policy(field, value):
    thread = sample("x" * 10000, archived=True)
    first = prepared(thread)
    changed = first.new_decisions[0].model_copy(update={field: value})
    with pytest.raises((KernelError, ValidationError)):
        prepared(thread, decisions=(changed,))


@pytest.mark.parametrize("mode", ["missing", "duplicate", "unknown", "orphan", "duplicate_result"])
def test_decisions_and_call_results_have_unique_complete_identity(mode):
    thread = sample("x", archived=True)
    first = prepared(thread)
    decisions = first.new_decisions
    if mode == "missing":
        decisions = ()
    elif mode == "duplicate":
        decisions *= 2
    elif mode == "unknown":
        decisions = (*decisions, decisions[0].model_copy(update={"item_id": uuid4()}))
    elif mode == "orphan":
        thread = with_turn(thread, items=thread.turns[0].items[::2])
    else:
        thread = with_turn(thread, items=(*thread.turns[0].items, thread.turns[0].items[-1]))
    with pytest.raises(KernelError):
        prepared(thread, decisions=decisions)


@pytest.mark.parametrize("field", [[], {}, None, True, "text"])
def test_omission_field_rejects_malformed_json_without_typeerror(field):
    decision = prepared(sample("x" * 10000, archived=True)).new_decisions[0]
    data = decision.model_dump(mode="json")
    data["replacement_output"]["model_view"]["omitted_field"] = field
    with pytest.raises(ValidationError):
        ToolResultViewDecision.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "change", ["step", "digest", "missing", "state", "duplicate", "old_schema"]
)
def test_history_event_validates_replay_and_rejects_invalid_transition(change):
    thread = sample("x" * 10000, archived=True)
    first = prepared(thread)
    payload = ModelHistoryPrepared(inspection=first.inspection, decisions=first.new_decisions)
    event = AgentEvent(
        thread_id=thread.thread_id,
        turn_id=thread.turns[0].turn_id,
        sequence=thread.sequence + 1,
        payload=payload,
    )
    accepted = apply_event(thread, event)
    assert accepted.turns[0].tool_result_view_decisions == first.new_decisions
    assert accepted.turns[0].items == thread.turns[0].items
    if change == "old_schema":
        with pytest.raises(ValidationError):
            EventDraft(schema_version=12, payload=payload)
        return
    if change == "step":
        payload = payload.model_copy(
            update={"inspection": first.inspection.model_copy(update={"model_step": 2})}
        )
    elif change == "digest":
        payload = payload.model_copy(
            update={
                "inspection": first.inspection.model_copy(update={"view_history_sha256": "f" * 64})
            }
        )
    elif change == "missing":
        payload = payload.model_copy(update={"decisions": ()})
    elif change == "state":
        thread = with_turn(thread, status=TurnStatus.CALLING_MODEL)
    elif change == "duplicate":
        thread = accepted
    event = event.model_copy(update={"sequence": thread.sequence + 1, "payload": payload})
    with pytest.raises(KernelError) as error:
        apply_event(thread, event)
    assert error.value.code == "invalid_event"


@pytest.mark.parametrize("kind", ["patch", "patch_batch", "process"])
def test_partial_effect_artifacts_cannot_replace_whole_large_result(kind):
    from harnessix.agent.models import PatchBatchEffect, PatchEffect, ProcessActionEffect
    from harnessix.domain.models import ActionStatus

    thread = sample("x" * 10000, archived=True)
    item = thread.turns[0].items[-1]
    fields = {
        "workspace_id": uuid4(),
        "request_id": "0" * 64,
        "approval_fingerprint": "1" * 64,
        "origin": "execution",
    }
    updates = {}
    if kind == "patch":
        effect = PatchEffect(**fields, plan_id=uuid4(), state="applied")
    elif kind == "patch_batch":
        effect = PatchBatchEffect(**fields, batch_id=uuid4())
    else:
        action = uuid4()
        effect = ProcessActionEffect(
            action_id=action,
            action_fingerprint="0" * 64,
            plan_fingerprint="1" * 64,
            status=ActionStatus.SUCCEEDED,
            result_fingerprint="2" * 64,
            origin="execution",
        )
        updates["action_id"] = action
    changed = item.model_copy(
        update={"content": item.content.model_copy(update={kind: effect, **updates})}
    )
    thread = with_turn(thread, items=(*thread.turns[0].items[:-1], changed))
    with pytest.raises(KernelError) as error:
        prepared(thread)
    assert error.value.code == "context_tool_result_unsupported"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "\ud800"])
def test_non_utf8_or_nonfinite_json_fails_with_controlled_error(value):
    with pytest.raises(KernelError) as error:
        prepared(sample(value))
    assert error.value.code == "context_tool_result_invalid"


def test_history_preparation_has_bounded_item_and_byte_limits():
    thread = sample(None)
    many = with_turn(thread, items=(thread.turns[0].items[0],) * 8193)
    huge_text = (
        thread.turns[0]
        .items[0]
        .model_copy(update={"content": TextContent(kind="user_message", text="中" * 1_000_000)})
    )
    big = with_turn(thread, items=(huge_text,) * 3)
    for excessive in (many, big):
        with pytest.raises(KernelError) as error:
            prepared(excessive)
        assert error.value.code == "context_budget_exceeded"
