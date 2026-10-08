"""私有纯声明夹具，不认证Session、不签发Writer或执行授权。"""

import inspect
import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from uuid import UUID

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentEvent,
    Thread,
    ThreadCreated,
)
from harnessix.agent.reducer import replay
from harnessix.domain.models import ApprovalOutcome
from harnessix.product_config.git_approval_history_proof import ApprovalHistoryEvidence
from harnessix.product_config.git_decision_link_sources import (
    build_git_decision_link_sources,
    session_event_refs,
)
from harnessix.product_config.git_decision_link_wire import (
    decode_product_git_decision_link,
    encode_product_git_decision_link,
)
from harnessix.product_config.git_prepared_link_proof import PreparedLinkEvidence
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.session.event_body_refs import EventBodyRef
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from tests.product_config.git_delivery_plan_support import NOW
from tests.product_config.test_git_approval_history_projection import Transcript
from tests.product_config.test_git_decision_link_contracts import case as case


def fixture(case, kind):
    """复用原 Reducer/历史投影；原字节引用仍是未签名纯声明夹具。"""
    transcript = Transcript(case)
    if kind == "cancelled":
        transcript.cancel()
    else:
        transcript.decide(
            ApprovalOutcome.APPROVED if kind == "approved" else ApprovalOutcome.REJECTED
        )
    events = tuple(transcript.events)
    refs = tuple(
        EventBodyRef(
            event.thread_id,
            event.event_id,
            event.sequence,
            sha256(event.model_dump_json().encode()).hexdigest(),
        )
        for event in events
    )
    history = AuthenticatedThreadHistory(transcript.thread, events, refs)
    return ApprovalHistoryEvidence(
        PreparedLinkEvidence(transcript.prepared, history, transcript.route, b""),
        transcript.interpret(),
        transcript.route_events,
        transcript.checkpoint,
        None,
    )


@pytest.mark.parametrize("kind", ["approved", "denied", "cancelled"])
def test_closed_three_declarations(case, kind):
    e = fixture(case, kind)
    d = build_git_decision_link_sources(e, checkpoint=lambda: None)
    assert d.fact_kind == kind
    assert (
        d.prepared_body_sha256
        == sha256(
            encode_product_git_prepared_link(e.materials.link, checkpoint=lambda: None)
        ).hexdigest()
    )
    assert (
        d.request_event.digest
        == e.materials.history.body_refs[d.request_event.sequence - 1].body_sha256
    )
    assert (
        decode_product_git_decision_link(
            encode_product_git_decision_link(d, checkpoint=lambda: None), checkpoint=lambda: None
        )
        == d
    )
    assert not any(hasattr(d, n) for n in ["token", "execute", "writer", "key"])


@pytest.mark.parametrize(
    "mutation",
    ["missing", "partial", "duplicate", "mixed_thread", "event_id", "sequence", "invalid_sha"],
)
def test_bad_source_refs_rejected(case, mutation):
    e = fixture(case, "approved")
    h = e.materials.history
    r = list(h.body_refs)
    if mutation == "missing":
        r = []
    if mutation == "partial":
        r = r[:1]
    if mutation == "duplicate":
        r[1] = r[0]
    if mutation == "mixed_thread":
        r[0] = replace(r[0], thread_id=UUID(int=1234))
    if mutation == "event_id":
        r[0] = replace(r[0], event_id=UUID(int=1234))
    if mutation == "sequence":
        r[0] = replace(r[0], sequence=7)
    if mutation == "invalid_sha":
        r[0] = replace(r[0], body_sha256="not-a-digest")
    with pytest.raises(KernelError):
        session_event_refs(replace(h, body_refs=tuple(r)), checkpoint=lambda: None)


def test_old_two_arg_history_semantic_only(case):
    h = fixture(case, "approved").materials.history
    old = AuthenticatedThreadHistory(h.thread, h.events)
    assert old.body_refs == () and old != h
    with pytest.raises(KernelError):
        session_event_refs(old, checkpoint=lambda: None)


def test_same_model_different_original_bytes_equality():
    event = AgentEvent(
        thread_id=UUID(int=7),
        event_id=UUID(int=8),
        sequence=1,
        occurred_at=NOW,
        payload=ThreadCreated(workspace="/fixture"),
    )
    raw = json.dumps(json.loads(event.model_dump_json()), indent=2).encode()
    assert (
        AgentEvent.model_validate_json(raw) == event
        and sha256(raw).hexdigest() != sha256(event.model_dump_json().encode()).hexdigest()
    )
    ref = EventBodyRef(event.thread_id, event.event_id, 1, sha256(raw).hexdigest())
    h = AuthenticatedThreadHistory(
        Thread(
            thread_id=event.thread_id,
            workspace="/fixture",
            created_at=NOW,
            updated_at=NOW,
            sequence=1,
        ),
        (event,),
        (ref,),
    )
    assert (
        session_event_refs(h, checkpoint=lambda: None)[event.event_id].digest
        == sha256(raw).hexdigest()
    )
    assert h != replace(
        h,
        body_refs=(replace(ref, body_sha256=sha256(event.model_dump_json().encode()).hexdigest()),),
    )
    with pytest.raises(FrozenInstanceError):
        ref.body_sha256 = "0" * 64


def test_pending_and_callback_refused(case):
    e = fixture(case, "approved")
    e = replace(e, projection=replace(e.projection, state="pending"))
    with pytest.raises(KernelError):
        build_git_decision_link_sources(e, checkpoint=lambda: None)
    error = OSError("safe sentinel")

    def fail():
        raise error

    with pytest.raises(OSError) as caught:
        session_event_refs(e.materials.history, checkpoint=fail)
    assert caught.value is error


@pytest.mark.parametrize("kind", ["approved", "denied", "cancelled"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError, OSError])
def test_original_checkpoint_reaches_closed_model_predecessor_validation(case, kind, error_type):
    evidence = fixture(case, kind)
    original = error_type("original-model-checkpoint")

    def checkpoint():
        frame = inspect.currentframe()
        try:
            while frame is not None:
                if (
                    frame.f_code.co_name == "complete_predecessor"
                    and frame.f_code.co_filename.endswith("git_decision_link_contracts.py")
                ):
                    raise original
                frame = frame.f_back
        finally:
            del frame

    with pytest.raises(error_type) as caught:
        build_git_decision_link_sources(evidence, checkpoint=checkpoint)
    assert caught.value is original


def test_original_semantic_projection_is_replayed_not_trusted_by_state_label(case):
    evidence = fixture(case, "approved")
    history = evidence.materials.history
    # 给根事件装上审批payload，定位格式仍完整，但已不属于原合法历史。
    first = history.events[0].model_copy(
        update={"payload": evidence.projection.request_event.payload}
    )
    changed_history = replace(history, events=(first, *history.events[1:]))
    forged = replace(evidence, materials=replace(evidence.materials, history=changed_history))
    with pytest.raises(KernelError):
        build_git_decision_link_sources(forged, checkpoint=lambda: None)


def test_malformed_non_target_content_cannot_be_replayed_as_valid_original_history(case):
    from harnessix.agent.models import ItemFinished, ItemStarted, TextContent

    evidence = fixture(case, "approved")
    history = evidence.materials.history
    changed = []
    for event in history.events:
        payload = event.payload
        if isinstance(payload, ItemStarted | ItemFinished) and isinstance(
            payload.content, TextContent
        ):
            payload = payload.model_copy(
                update={"content": payload.content.model_copy(update={"text": 123})}
            )
            event = event.model_copy(update={"payload": payload})
        changed.append(event)
    assert any(event != original for event, original in zip(changed, history.events, strict=True))
    malformed = replace(history, thread=replay(tuple(changed)), events=tuple(changed))
    forged = replace(evidence, materials=replace(evidence.materials, history=malformed))
    with pytest.raises(KernelError):
        build_git_decision_link_sources(forged, checkpoint=lambda: None)


def test_foreign_identity_rejected_before_event_comparison(case):
    evidence = fixture(case, "approved")
    history = evidence.materials.history
    calls = []

    class FakeId(str):
        def __eq__(self, other):
            calls.append(other)
            return True

    event = history.events[0]
    forged = replace(
        history, events=(event.model_copy(update={"event_id": FakeId("fake")}), *history.events[1:])
    )
    with pytest.raises(KernelError):
        session_event_refs(forged, checkpoint=lambda: None)
    assert not calls


def test_foreign_approval_identity_cannot_invoke_equality_in_replay(case):
    from harnessix.agent.models import ItemStarted, TrustedActionApprovalRequestContent

    evidence = fixture(case, "approved")
    history = evidence.materials.history
    calls = []

    class FakeId(str):
        def __eq__(self, other):
            calls.append(other)
            return True

    events = list(history.events)
    index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event.payload, ItemStarted)
        and isinstance(event.payload.content, TrustedActionApprovalRequestContent)
    )
    payload = events[index].payload
    content = payload.content.model_copy(update={"approval_id": FakeId("fake")})
    events[index] = events[index].model_copy(
        update={"payload": payload.model_copy(update={"content": content})}
    )
    with pytest.raises(KernelError):
        session_event_refs(replace(history, events=tuple(events)), checkpoint=lambda: None)
    assert not calls


def test_original_budget_default_has_declared_float_type_without_changing_limit():
    from harnessix.agent.models import Budget

    budget = Budget()
    assert type(budget.timeout_seconds) is float and budget.timeout_seconds == 120
    assert Budget.model_validate({"timeout_seconds": 120}, strict=True) == budget


def test_foreign_turn_identity_rejected_without_calling_its_comparison(case):
    evidence = fixture(case, "approved")
    history = evidence.materials.history
    calls = []

    class FakeUUID(UUID):
        def __eq__(self, other):
            calls.append(other)
            return True

        __hash__ = UUID.__hash__

    events = list(history.events)
    index = next(index for index, event in enumerate(events) if event.turn_id is not None)
    events[index] = events[index].model_copy(update={"turn_id": FakeUUID(int=999)})
    with pytest.raises(KernelError):
        session_event_refs(replace(history, events=tuple(events)), checkpoint=lambda: None)
    assert not calls


def test_legacy_serializer_must_not_hide_disallowed_original_fields(case):
    from harnessix.agent.models import TurnStateChanged

    evidence = fixture(case, "approved")
    history = evidence.materials.history
    events = list(history.events)
    index = next(
        index for index, event in enumerate(events) if isinstance(event.payload, TurnStateChanged)
    )
    event = events[index]
    events[index] = event.model_copy(
        update={
            "schema_version": 18,
            "payload": event.payload.model_copy(update={"reason": "context_overflow"}),
        }
    )
    # 旧 serializer 隐去 reason；校验该投影会错误地恢复 normal。
    projected = AgentEvent.model_validate_json(events[index].model_dump_json(), strict=True)
    assert projected.payload.reason == "normal"
    with pytest.raises(KernelError):
        session_event_refs(replace(history, events=tuple(events)), checkpoint=lambda: None)


@pytest.mark.parametrize("stage", ["first", "inside", "last"])
@pytest.mark.parametrize(
    "error",
    [ValueError("original"), TypeError("original"), OSError("original"), TurnCancelled()],
)
def test_mapper_preserves_original_control_error_identity_at_every_stage(case, stage, error):
    evidence = fixture(case, "approved")
    calls = 0

    def count():
        nonlocal calls
        calls += 1

    build_git_decision_link_sources(evidence, checkpoint=count)
    target = {"first": 1, "inside": 150, "last": calls}[stage]
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == target:
            raise error

    with pytest.raises(type(error)) as caught:
        build_git_decision_link_sources(evidence, checkpoint=checkpoint)
    assert caught.value is error and calls == target


@pytest.mark.parametrize(
    "mutation",
    ["materials_type", "projection_type", "missing_checkpoint", "route_prefix", "label", "request"],
)
def test_mapper_refuses_inconsistent_evidence_without_new_authority(case, mutation):
    evidence = fixture(case, "approved")
    if mutation == "materials_type":
        evidence = replace(evidence, materials=None)
    elif mutation == "projection_type":
        evidence = replace(evidence, projection=None)
    elif mutation == "missing_checkpoint":
        evidence = replace(evidence, approval=None)
    elif mutation == "route_prefix":
        evidence = replace(evidence, route_events=evidence.route_events[1:])
    elif mutation == "label":
        evidence = replace(evidence, projection=replace(evidence.projection, state="denied"))
    else:
        request = evidence.projection.request_event.model_copy(update={"sequence": 999999})
        evidence = replace(evidence, projection=replace(evidence.projection, request_event=request))
    with pytest.raises(KernelError):
        build_git_decision_link_sources(evidence, checkpoint=lambda: None)


def test_formatted_caller_hash_is_only_a_declaration_not_a_mac_proof(case):
    """刻意展示普通聚合可构造，不能把此内部映射结果当成认证能力。"""
    evidence = fixture(case, "approved")
    history = evidence.materials.history
    declared = replace(
        history,
        body_refs=tuple(replace(ref, body_sha256="0" * 64) for ref in history.body_refs),
    )
    evidence = replace(evidence, materials=replace(evidence.materials, history=declared))
    result = build_git_decision_link_sources(evidence, checkpoint=lambda: None)
    assert result.request_event.digest == "0" * 64
    assert not any(hasattr(result, name) for name in ("verified", "can_execute", "issue", "key"))


@pytest.mark.parametrize(
    "part,field",
    [
        ("ref", "thread_id"),
        ("ref", "event_id"),
        ("ref", "sequence"),
        ("ref", "body_sha256"),
        ("event", "thread_id"),
        ("event", "event_id"),
        ("event", "sequence"),
        ("thread", "thread_id"),
        ("thread", "sequence"),
    ],
)
def test_identity_fields_reject_foreign_equality_before_invoking_it(case, part, field):
    history = fixture(case, "approved").materials.history
    calls = []

    class ForeignEqual:
        def __eq__(self, other):
            calls.append(other)
            return True

    foreign = ForeignEqual()
    if part == "ref":
        history = replace(
            history,
            body_refs=(replace(history.body_refs[0], **{field: foreign}), *history.body_refs[1:]),
        )
    elif part == "event":
        first = history.events[0].model_copy(update={field: foreign})
        history = replace(history, events=(first, *history.events[1:]))
    else:
        history = replace(history, thread=history.thread.model_copy(update={field: foreign}))
    with pytest.raises(KernelError):
        session_event_refs(history, checkpoint=lambda: None)
    assert calls == []
