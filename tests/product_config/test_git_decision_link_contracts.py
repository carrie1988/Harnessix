"""决定事实的纯声明与规范字节；测试索引不代表认证历史或执行授权。"""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.execution.contracts import ExecutionApprovalCheckpoint
from harnessix.product_config.git_decision_link_contracts import (
    GitSessionEventRef,
    ProductGitApprovedLink,
    ProductGitCancelledLink,
    ProductGitDeniedLink,
    snapshot_product_git_decision_link,
)
from harnessix.product_config.git_decision_link_wire import (
    decode_product_git_decision_link,
    encode_product_git_decision_link,
)
from harnessix.product_config.git_delivery_plan_wire import MAX_PRODUCT_GIT_PLAN_BYTES
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_wire import (
    decode_product_git_prepared_link,
    encode_product_git_prepared_link,
)
from tests.product_config.git_delivery_plan_support import NOW
from tests.product_config.test_git_prepared_link_contracts import approval_for, field_paths
from tests.support.git_delivery_observed_core import canonical, json_facts, make_observed_case


@pytest.fixture
def case(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield make_observed_case(GitMaterialCAS(store), tmp_path)[0]


def event(number):
    return GitSessionEventRef(event_id=UUID(int=number), sequence=number, digest="1" * 64)


def declaration(case, fact_kind="approved"):
    request = approval_for(case)
    outcome = ApprovalOutcome.APPROVED if fact_kind == "approved" else ApprovalOutcome.REJECTED
    record = ApprovalRecord(
        outcome=outcome,
        actor="system.cancel" if fact_kind == "cancelled" else "operator",
        reason="turn_cancelled" if fact_kind == "cancelled" else "reviewed",
        request_fingerprint=case.plan.route.execution.fingerprint,
        decided_at=NOW,
    )
    checkpoint = ExecutionApprovalCheckpoint(
        plan_id=case.plan.route.execution.plan_id,
        plan_fingerprint=case.plan.route.execution.fingerprint,
        decision=record,
    )
    common = dict(
        plan=case.plan,
        approval_request=request,
        request_event=event(1),
        prepared_body_sha256="2" * 64,
        router_approval=checkpoint,
        route_decision_sequence=2,
        route_decision_digest="3" * 64,
    )
    if fact_kind == "cancelled":
        return ProductGitCancelledLink(
            **common,
            cancel_event=event(2),
            approval_cancel_event=event(3),
            call_result_event=event(4),
            turn_terminal_event=event(5),
        )
    decided = request.model_copy(
        update={
            "decision": record.model_copy(
                update={"request_fingerprint": request.request_fingerprint}
            ),
            "route_state": "ready" if fact_kind == "approved" else "denied",
        }
    )
    kind = ProductGitApprovedLink if fact_kind == "approved" else ProductGitDeniedLink
    return kind(**common, session_decision=decided, decision_event=event(2))


@pytest.mark.parametrize("fact_kind", ["approved", "denied", "cancelled"])
def test_roundtrip_is_complete_data_not_authorization(case, fact_kind):
    fact = declaration(case, fact_kind)
    body = encode_product_git_decision_link(fact, checkpoint=lambda: None)
    assert body == canonical(json_facts(fact))
    reopened = decode_product_git_decision_link(body, checkpoint=lambda: None)
    assert reopened == fact and reopened is not fact
    assert reopened.plan is not fact.plan and reopened.approval_request is not fact.approval_request
    assert fact.phase == ("approved" if fact_kind == "approved" else "failed")
    assert fact.sequence == 1
    assert not any(hasattr(fact, name) for name in ("execute", "can_execute", "owner", "key"))


@pytest.mark.parametrize("sequence", [True, False, 1.0, "1", 0, 2])
def test_sequence_requires_exact_integer_one(case, sequence):
    fact = declaration(case).model_copy(update={"sequence": sequence})
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(fact, checkpoint=lambda: None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "failed"),
        ("fact_kind", "denied"),
        ("prepared_body_sha256", "private-sensitive"),
        ("route_decision_sequence", False),
    ],
)
def test_corrupt_outer_declaration_refused(case, field, value):
    bad = declaration(case).model_copy(update={field: value})
    with pytest.raises(KernelError):
        encode_product_git_decision_link(bad, checkpoint=lambda: None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor", "another"),
        ("reason", "different"),
        ("request_fingerprint", "4" * 64),
        ("outcome", ApprovalOutcome.REJECTED),
    ],
)
def test_session_and_execution_decisions_must_match_separate_domains(case, field, value):
    fact = declaration(case)
    decision = fact.session_decision.decision.model_copy(update={field: value})
    bad = fact.model_copy(
        update={"session_decision": fact.session_decision.model_copy(update={"decision": decision})}
    )
    with pytest.raises(KernelError):
        encode_product_git_decision_link(bad, checkpoint=lambda: None)


@pytest.mark.parametrize("field", ["approval_id", "call_id", "plan_id", "request_fingerprint"])
def test_decision_cannot_replace_original_request_fields(case, field):
    fact = declaration(case)
    value = "4" * 64 if field == "request_fingerprint" else UUID(int=444)
    decided = fact.session_decision.model_copy(update={field: value})
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(
            fact.model_copy(update={"session_decision": decided}), checkpoint=lambda: None
        )


@pytest.mark.parametrize("sequence,event_id", [(1, 2), (2, 1)])
def test_request_and_decision_event_indices_are_distinct_ordered(case, sequence, event_id):
    fact = declaration(case)
    bad = fact.model_copy(
        update={
            "decision_event": event(2).model_copy(
                update={"sequence": sequence, "event_id": UUID(int=event_id)}
            )
        }
    )
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(bad, checkpoint=lambda: None)


def test_cancelled_variant_forbids_human_session_decision(case):
    fact = declaration(case, "cancelled")
    fields = {k: getattr(fact, k) for k in type(fact).model_fields}
    with pytest.raises(ValidationError):
        ProductGitCancelledLink(**fields, session_decision=declaration(case).session_decision)


@pytest.mark.parametrize(
    "field,value",
    [("actor", "operator"), ("reason", "different"), ("outcome", ApprovalOutcome.APPROVED)],
)
def test_cancelled_requires_original_negative_checkpoint_shape(case, field, value):
    fact = declaration(case, "cancelled")
    cp = fact.router_approval.model_copy(
        update={"decision": fact.router_approval.decision.model_copy(update={field: value})}
    )
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(
            fact.model_copy(update={"router_approval": cp}), checkpoint=lambda: None
        )


@pytest.mark.parametrize(
    "field", ["cancel_event", "approval_cancel_event", "call_result_event", "turn_terminal_event"]
)
def test_cancelled_rejects_reused_or_unordered_event_indices(case, field):
    fact = declaration(case, "cancelled").model_copy(update={field: event(1)})
    with pytest.raises(KernelError):
        encode_product_git_decision_link(fact, checkpoint=lambda: None)


@pytest.mark.parametrize(
    "mutation", ["extra", "missing", "duplicate", "whitespace", "nonfinite", "old_version"]
)
def test_wire_rejects_noncanonical_or_unknown_body(case, mutation):
    fact = declaration(case)
    data = json_facts(fact)
    if mutation == "extra":
        data["can_execute"] = True
    if mutation == "missing":
        del data["sequence"]
    if mutation == "old_version":
        data["spec_version"] = "harnessix.product-git-prepared-link/v1"
    body = canonical(data)
    if mutation == "duplicate":
        body = body[:-1] + b',"sequence":1}'
    if mutation == "whitespace":
        body += b" "
    if mutation == "nonfinite":
        body = body.replace(b'"sequence":1', b'"sequence":NaN')
    with pytest.raises(KernelError) as caught:
        decode_product_git_decision_link(body, checkpoint=lambda: None)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert "private-sensitive" not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize("error", [TurnCancelled(), OSError("private-sensitive"), TimeoutError()])
def test_control_exceptions_preserve_identity(case, error):
    fact = declaration(case)
    for op, value in (
        (snapshot_product_git_decision_link, fact),
        (encode_product_git_decision_link, fact),
        (decode_product_git_decision_link, canonical(json_facts(fact))),
    ):

        def fail():
            raise error

        with pytest.raises(type(error)) as caught:
            op(value, checkpoint=fail)
        assert caught.value is error


def test_mutable_aliases_and_nested_construct_bypass_refused(case):
    fact = declaration(case)
    bad = copy.deepcopy(fact)
    object.__setattr__(bad.router_approval.decision, "actor", 123)
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(bad, checkpoint=lambda: None)
    assert fact.router_approval.decision.actor == "operator"


@pytest.mark.parametrize("fact_kind", ["approved", "denied", "cancelled"])
def test_every_wire_field_is_required_even_when_model_has_default(case, fact_kind):
    """逐字段删除包含嵌套默认项；解码不得用默认值修补持久事实。"""
    payload = json_facts(declaration(case, fact_kind))
    paths = tuple(field_paths(payload))
    assert len(paths) > 350
    for path in paths:
        changed = copy.deepcopy(payload)
        parent = changed
        for name in path[:-1]:
            parent = parent[name]
        del parent[path[-1]]
        with pytest.raises(KernelError):
            decode_product_git_decision_link(canonical(changed), checkpoint=lambda: None)


@pytest.mark.parametrize("fact_kind", ["approved", "denied", "cancelled"])
@pytest.mark.parametrize("fault", ["root-subclass", "ref-subclass", "extra", "missing", "ref-bool"])
def test_strict_snapshot_refuses_foreign_types_and_hidden_fields(case, fact_kind, fault):
    fact = declaration(case, fact_kind).model_copy(deep=True)
    if fault in {"root-subclass", "ref-subclass"}:
        original = fact if fault == "root-subclass" else fact.request_event
        foreign = type("ForeignDeclaration", (type(original),), {})
        child = foreign.model_construct(**vars(original))
        if fault == "root-subclass":
            fact = child
        else:
            fact.__dict__["request_event"] = child
    elif fault == "extra":
        fact.router_approval.decision.__dict__["private-sensitive"] = "not-a-field"
    elif fault == "missing":
        fact.__dict__.pop("sequence")
    else:
        fact.request_event.__dict__["sequence"] = True
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(fact, checkpoint=lambda: None)


@pytest.mark.parametrize("field", ["plan_id", "plan_fingerprint", "request_fingerprint", "time"])
def test_execution_checkpoint_binds_its_own_original_fingerprint_and_time(case, field):
    fact = declaration(case)
    cp = fact.router_approval
    if field in {"plan_id", "plan_fingerprint"}:
        cp = cp.model_copy(update={field: UUID(int=999) if field == "plan_id" else "4" * 64})
    else:
        update = (
            {"request_fingerprint": fact.approval_request.request_fingerprint}
            if field == "request_fingerprint"
            else {"decided_at": NOW + timedelta(seconds=1)}
        )
        cp = cp.model_copy(update={"decision": cp.decision.model_copy(update=update)})
    with pytest.raises(KernelError):
        encode_product_git_decision_link(
            fact.model_copy(update={"router_approval": cp}), checkpoint=lambda: None
        )


@pytest.mark.parametrize("fact_kind", ["approved", "denied", "cancelled"])
def test_naive_decision_time_refused_without_replacing_original_contract(case, fact_kind):
    fact = declaration(case, fact_kind)
    cp = fact.router_approval.model_copy(
        update={
            "decision": fact.router_approval.decision.model_copy(
                update={"decided_at": NOW.replace(tzinfo=None)}
            )
        }
    )
    with pytest.raises(KernelError):
        snapshot_product_git_decision_link(
            fact.model_copy(update={"router_approval": cp}), checkpoint=lambda: None
        )


@pytest.mark.parametrize("kind", ["str", "bytearray", "memoryview", "subclass", "none", "oversize"])
def test_transport_requires_bounded_exact_bytes(case, kind):
    class ForeignBytes(bytes):
        pass

    body = canonical(json_facts(declaration(case)))
    value = {
        "str": body.decode(),
        "bytearray": bytearray(body),
        "memoryview": memoryview(body),
        "subclass": ForeignBytes(body),
        "none": None,
        "oversize": b" " * (MAX_PRODUCT_GIT_PLAN_BYTES + 1),
    }[kind]
    with pytest.raises(KernelError):
        decode_product_git_decision_link(value, checkpoint=lambda: None)


@pytest.mark.parametrize(
    "operation",
    [
        snapshot_product_git_decision_link,
        encode_product_git_decision_link,
        decode_product_git_decision_link,
    ],
)
@pytest.mark.parametrize("stage", ["first", "inside", "last"])
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("original"),
        TypeError("original"),
        ValidationError.from_exception_data("original", []),
        KernelError("original", "原检查点终止"),
        TurnCancelled(),
        TimeoutError(),
        asyncio.CancelledError(),
    ],
)
def test_checkpoint_exception_identity_at_all_stages(case, operation, stage, failure):
    fact = declaration(case)
    value = canonical(json_facts(fact)) if operation is decode_product_git_decision_link else fact
    total = 0

    def count():
        nonlocal total
        total += 1

    operation(value, checkpoint=count)
    at = {"first": 1, "inside": 150, "last": total}[stage]
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == at:
            raise failure

    with pytest.raises(type(failure)) as caught:
        operation(value, checkpoint=check)
    assert caught.value is failure and calls == at


@pytest.mark.parametrize("fact_kind", ["approved", "denied", "cancelled"])
def test_prepared_wire_stays_pending_only_and_cannot_decode_decision(case, fact_kind):
    fact = declaration(case, fact_kind)
    pending = ProductGitPreparedLink(plan=fact.plan, approval=fact.approval_request)
    prepared_body = encode_product_git_prepared_link(pending, checkpoint=lambda: None)
    assert decode_product_git_prepared_link(prepared_body, checkpoint=lambda: None) == pending
    with pytest.raises(KernelError):
        decode_product_git_prepared_link(canonical(json_facts(fact)), checkpoint=lambda: None)
    with pytest.raises(KernelError):
        decode_product_git_decision_link(prepared_body, checkpoint=lambda: None)


def test_valid_construct_is_only_revalidated_data_not_provenance(case):
    fact = declaration(case)
    constructed = ProductGitApprovedLink.model_construct(**vars(fact))
    snap = snapshot_product_git_decision_link(constructed, checkpoint=lambda: None)
    assert snap == fact and snap is not constructed
    assert snap.plan.core.call.arguments is not constructed.plan.core.call.arguments
    assert snap.session_decision.decision is not constructed.session_decision.decision


@pytest.mark.parametrize("fact_kind", ["approved", "denied", "cancelled"])
def test_exact_512k_complete_decision_body_and_one_byte_over(tmp_path, fact_kind):
    """合法 Commit 的完整 UTF-8 字节边界；不靠调低测试预算或裁剪字段。"""
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:

        def sized(path, message, provider="p"):
            case = make_observed_case(
                GitMaterialCAS(store),
                path,
                action="commit",
                commit_message=message,
                provider_call_id=provider,
            )[0]
            fact = declaration(case, fact_kind)
            return fact, canonical(json_facts(fact))

        message = "a" * 65535 + "\n"
        _, small = sized(tmp_path / "size-0000", message)
        assert small.count(canonical(message)) == 4
        extra = (MAX_PRODUCT_GIT_PLAN_BYTES - len(small) - 64) // 4
        emojis, accents = divmod(extra, 3)
        message = "🙂" * emojis + "é" * accents + "a" * (65535 - emojis - accents) + "\n"
        _, near = sized(tmp_path / "size-0001", message)
        padding = MAX_PRODUCT_GIT_PLAN_BYTES - len(near)
        assert 0 <= padding <= 254
        exact, body = sized(tmp_path / "size-0002", message, "p" * (padding + 1))
        assert len(body) == MAX_PRODUCT_GIT_PLAN_BYTES
        assert encode_product_git_decision_link(exact, checkpoint=lambda: None) == body
        assert decode_product_git_decision_link(body, checkpoint=lambda: None) == exact
        over, body = sized(tmp_path / "size-0003", message, "p" * (padding + 2))
        assert len(body) == MAX_PRODUCT_GIT_PLAN_BYTES + 1
        for op, value in (
            (encode_product_git_decision_link, over),
            (decode_product_git_decision_link, body),
        ):
            with pytest.raises(KernelError):
                op(value, checkpoint=lambda: None)
