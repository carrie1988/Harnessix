"""prepared 关联的正式数据合同与规范字节；真实 CAS 夹具不代表业务认证。"""

from __future__ import annotations

import asyncio
import copy
import json
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, Thread, TrustedActionApprovalRequestContent, Turn
from harnessix.agent.trusted_action_contracts import TrustedActionReview
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord
from harnessix.product_config import git_prepared_link_contracts as contracts_module
from harnessix.product_config import git_prepared_link_wire as wire_module
from harnessix.product_config.git_delivery_observed_contracts import ProductGitDeliveryPlanV2
from harnessix.product_config.git_delivery_observed_wire import (
    decode_product_git_delivery_plan_v2,
    encode_product_git_delivery_plan_v2,
)
from harnessix.product_config.git_delivery_plan_wire import (
    MAX_PRODUCT_GIT_PLAN_BYTES,
    decode_product_git_delivery_plan,
    encode_product_git_delivery_plan,
)
from harnessix.product_config.git_prepared_link_contracts import (
    ProductGitPreparedLink,
    snapshot_product_git_prepared_link,
)
from harnessix.product_config.git_prepared_link_wire import (
    decode_product_git_prepared_link,
    encode_product_git_prepared_link,
)
from harnessix.trusted_actions.agent_gateway_output import build_approval
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2
from tests.product_config.git_delivery_plan_support import NOW, observe_state
from tests.support.git_delivery_observed_core import (
    canonical,
    json_facts,
    make_observed_case,
    plan_payload,
)


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


def approval_for(case):
    """普通 Thread/Turn 声明只用于调用原构建器，不伪造认证 Session 或批准。"""
    core = case.core
    thread = Thread(
        thread_id=core.thread_id,
        workspace=str(case.workspace_root),
        created_at=NOW,
        updated_at=NOW,
    )
    turn = Turn(
        turn_id=core.turn_id,
        request_id="contract-fixture",
        request_fingerprint="0" * 64,
        budget=Budget(),
        created_at=NOW,
    )
    route = ActionRouteSnapshotV2(
        plan=case.plan.route,
        state="pending_approval",
        sequence=1,
        last_event_digest="0" * 64,
        updated_at=NOW,
    )
    return build_approval(
        thread,
        turn,
        core.call,
        route,
        TrustedActionReview(diff_artifact=case.plan.review_artifact),
        presentation="patch_batch",
    )


def link_for(case):
    return ProductGitPreparedLink(plan=case.plan, approval=approval_for(case))


@pytest.fixture
def case(cas, tmp_path):
    return make_observed_case(cas, tmp_path)[0]


@pytest.fixture
def link(case):
    return link_for(case)


def payload_for(link):
    """预期逐字段取原声明，由独立 Oracle 生成，不依赖受测序列化器。"""
    return {
        "spec_version": "harnessix.product-git-prepared-link/v1",
        "plan": plan_payload(link.plan),
        "approval": json_facts(link.approval),
        "phase": "prepared",
        "sequence": 0,
    }


def reject(operation, value):
    """固定原领域拒绝不得携带正文、路径或底层错误，控制异常另行核验。"""
    with pytest.raises(KernelError) as caught:
        operation(value, checkpoint=lambda: None)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert caught.value.__cause__ is None
    assert "新代码" not in str(caught.value)
    assert "private-sensitive" not in str(caught.value)
    return caught.value


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_full_prepared_roundtrip_with_original_approval(cas, tmp_path, fmt, action, platform):
    """全部材料与原审批构建器往返；Windows 参数只声明逻辑元数据。"""
    case, _legacy = make_observed_case(cas, tmp_path, fmt=fmt, action=action, platform=platform)
    original = approval_for(case)
    link = ProductGitPreparedLink(plan=case.plan, approval=original)
    before = observe_state(case)
    expected = canonical(payload_for(link))
    body = encode_product_git_prepared_link(link, checkpoint=lambda: None)
    assert type(body) is bytes and body == expected
    restored = decode_product_git_prepared_link(body, checkpoint=lambda: None)
    assert type(restored) is ProductGitPreparedLink
    assert restored == link and restored is not link
    assert type(restored.plan) is ProductGitDeliveryPlanV2
    assert type(restored.approval) is TrustedActionApprovalRequestContent
    assert restored.approval == original
    execution = restored.plan.route.execution
    assert restored.approval.plan_id == execution.plan_id
    assert restored.approval.plan_fingerprint == restored.plan.route.fingerprint
    assert restored.approval.plan_fingerprint != restored.plan.fingerprint
    assert restored.approval.approval_id == uuid5(
        execution.plan_id, "harnessix.agent-trusted-action-approval/v1"
    )
    assert restored.approval.diff_artifact == restored.plan.review_artifact
    assert restored.approval.diff_artifact.sha256 != restored.plan.core.diff_sha256
    assert restored.phase == "prepared" and type(restored.sequence) is int
    assert restored.sequence == 0 and restored.approval.decision is None
    assert restored.approval.route_state == "pending_approval"
    assert restored.plan.core.object_scope == case.core.object_scope
    assert restored.plan.core.object_scope is not case.core.object_scope
    assert restored.plan.core.user_observation == case.core.user_observation
    assert restored.plan.core.baseline.source.workspace.parent_closure == (
        case.core.baseline.source.workspace.parent_closure
    )
    assert observe_state(case) == before
    assert not any(hasattr(restored, name) for name in ("approved", "authorized", "mac_verified"))


def test_exact_contract_fields_and_schema_do_not_add_later_states(link):
    assert set(ProductGitPreparedLink.model_fields) == {
        "spec_version",
        "plan",
        "approval",
        "phase",
        "sequence",
    }
    assert ProductGitPreparedLink.model_fields["plan"].annotation is ProductGitDeliveryPlanV2
    assert ProductGitPreparedLink.model_fields["approval"].annotation is (
        TrustedActionApprovalRequestContent
    )
    schema = ProductGitPreparedLink.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["phase"]["const"] == "prepared"
    assert schema["properties"]["sequence"]["const"] == 0
    assert schema["properties"]["sequence"]["type"] == "integer"
    assert schema["$defs"]["ProductGitDeliveryPlanV2"]["additionalProperties"] is False
    assert schema["$defs"]["TrustedActionApprovalRequestContent"]["additionalProperties"] is False
    with pytest.raises(ValidationError):
        link.phase = "succeeded"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec_version", "harnessix.product-git-prepared-link/v2"),
        ("phase", "approved"),
        ("phase", "succeeded"),
        ("phase", "materials_ready"),
        ("sequence", 1),
        ("sequence", -1),
        ("sequence", False),
        ("sequence", 0.0),
        ("sequence", "0"),
    ],
)
def test_no_later_phase_sequence_or_scalar_coercion(link, field, value):
    payload = payload_for(link)
    payload[field] = value
    reject(decode_product_git_prepared_link, canonical(payload))
    reject(encode_product_git_prepared_link, link.model_copy(update={field: value}))
    with pytest.raises(ValidationError):
        ProductGitPreparedLink(plan=link.plan, approval=link.approval, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approval_id", UUID(int=999)),
        ("call_id", UUID(int=999)),
        ("plan_id", UUID(int=999)),
        ("presentation", "tool"),
        ("plan_fingerprint", "0" * 64),
        ("execution_fingerprint", "0" * 64),
        ("policy_id", "another-policy"),
        ("policy_version", "another-version"),
        ("request_fingerprint", "not-a-revision"),
        ("request_fingerprint", "A" * 64),
    ],
)
def test_approval_binding_mismatch_is_rejected_at_every_boundary(link, field, value):
    approval = link.approval.model_copy(update={field: value})
    forged = link.model_copy(update={"approval": approval})
    reject(snapshot_product_git_prepared_link, forged)
    reject(encode_product_git_prepared_link, forged)
    reject(decode_product_git_prepared_link, canonical(json_facts(forged)))
    with pytest.raises((KernelError, ValidationError)):
        ProductGitPreparedLink(plan=link.plan, approval=approval)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_id", UUID(int=999)),
        ("sha256", "0" * 64),
        ("size_bytes", 999),
        ("records", 2),
        ("complete", False),
        ("expires_at", NOW.replace(day=9)),
    ],
)
def test_diff_artifact_equality_covers_every_original_field(link, field, value):
    changed = link.approval.diff_artifact.model_copy(update={field: value})
    approval = link.approval.model_copy(update={"diff_artifact": changed})
    forged = link.model_copy(update={"approval": approval})
    reject(encode_product_git_prepared_link, forged)
    reject(decode_product_git_prepared_link, canonical(json_facts(forged)))


@pytest.mark.parametrize("state", ["ready", "denied", "running", "succeeded", "unknown"])
def test_prepared_cannot_claim_router_transition(link, state):
    approval = link.approval.model_copy(update={"route_state": state})
    reject(encode_product_git_prepared_link, link.model_copy(update={"approval": approval}))
    payload = payload_for(link)
    payload["approval"]["route_state"] = state
    reject(decode_product_git_prepared_link, canonical(payload))


@pytest.mark.parametrize("outcome", [ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED])
def test_even_a_valid_original_decision_is_not_prepared(link, outcome):
    decision = ApprovalRecord(
        outcome=outcome,
        actor="contract-actor",
        request_fingerprint=link.approval.request_fingerprint,
        decided_at=NOW,
    )
    # 原审批合同可接受这个决定；prepared 合同不能由此扩展为批准记录。
    approval = TrustedActionApprovalRequestContent(
        **{
            **vars(link.approval),
            "decision": decision,
            "route_state": "denied" if outcome is ApprovalOutcome.REJECTED else "ready",
        }
    )
    forged = link.model_copy(update={"approval": approval})
    reject(encode_product_git_prepared_link, forged)
    reject(decode_product_git_prepared_link, canonical(json_facts(forged)))


def test_request_fingerprint_is_a_declaration_not_second_authentication_algorithm(
    link, monkeypatch
):
    """纯合同保留格式合法声明；完整会话认证与原请求重建必须由宿主完成。"""

    def forbidden(*args, **kwargs):
        pytest.fail("纯合同不得调用原 Session 认证或另行重建请求指纹")

    monkeypatch.setattr("harnessix.agent.approvals.trusted_action_request_fingerprint", forbidden)
    monkeypatch.setattr(
        "harnessix.trusted_actions.agent_gateway_output.trusted_action_request_fingerprint",
        forbidden,
    )
    approval = link.approval.model_copy(update={"request_fingerprint": "0" * 64})
    declaration = ProductGitPreparedLink(plan=link.plan, approval=approval)
    expected = canonical(payload_for(declaration))
    assert encode_product_git_prepared_link(declaration, checkpoint=lambda: None) == expected
    assert decode_product_git_prepared_link(expected, checkpoint=lambda: None) == declaration


@pytest.mark.parametrize(
    "operation", [snapshot_product_git_prepared_link, encode_product_git_prepared_link]
)
@pytest.mark.parametrize(
    "fault",
    [
        "root-subclass",
        "plan-subclass",
        "approval-subclass",
        "artifact-subclass",
        "root-extra",
        "root-extra-storage",
        "root-missing",
        "plan-extra",
        "plan-missing",
        "approval-extra",
        "approval-missing",
        "artifact-missing",
        "core-fingerprint",
        "observation-fingerprint",
        "arguments-scalar",
        "workspace-dict",
        "policy-scalar",
        "scope-subclass",
        "scope-list",
        "scope-metric",
        "tree-name",
    ],
)
def test_original_snapshot_refuses_construct_copy_and_deep_tampering(link, operation, fault):
    forged = link.model_copy(deep=True)
    if fault.endswith("subclass") and fault != "scope-subclass":
        if fault == "root-subclass":
            original = forged
        elif fault == "plan-subclass":
            original = forged.plan
        elif fault == "approval-subclass":
            original = forged.approval
        else:
            original = forged.approval.diff_artifact
        kind = type("ForeignContract", (type(original),), {})
        child = kind.model_construct(**vars(original))
        if fault == "root-subclass":
            forged = child
        elif fault == "plan-subclass":
            forged.__dict__["plan"] = child
        elif fault == "approval-subclass":
            forged.__dict__["approval"] = child
        else:
            forged.approval.__dict__["diff_artifact"] = child
    elif fault == "root-extra-storage":
        object.__setattr__(forged, "__pydantic_extra__", {})
    elif fault.endswith("extra"):
        original = {
            "root-extra": forged,
            "plan-extra": forged.plan,
            "approval-extra": forged.approval,
        }[fault]
        original.__dict__["undeclared"] = "private-sensitive"
    elif fault.endswith("missing"):
        original, name = {
            "root-missing": (forged, "sequence"),
            "plan-missing": (forged.plan, "spec_version"),
            "approval-missing": (forged.approval, "decision"),
            "artifact-missing": (forged.approval.diff_artifact, "complete"),
        }[fault]
        original.__dict__.pop(name)
    elif fault == "core-fingerprint":
        forged.plan.core.__dict__["fingerprint"] = "0" * 64
    elif fault == "observation-fingerprint":
        forged.plan.core.user_observation.__dict__["fingerprint"] = "0" * 64
    elif fault == "arguments-scalar":
        forged.plan.core.call.arguments["patches"] = (str(UUID(int=10)),)
    elif fault == "workspace-dict":
        forged.plan.route.execution.__dict__["workspace"] = {}
    elif fault == "policy-scalar":
        forged.plan.route.execution.policy.__dict__["version"] = 1
    else:
        scope = forged.plan.core.object_scope
        if fault == "scope-subclass":
            kind = type("ForeignScope", (GitInventoryScope,), {})
            # 原 dataclass 构造器已拒绝子类；原始分配模拟绕过构造器的伪造对象。
            child = object.__new__(kind)
            for name in scope.__dataclass_fields__:
                object.__setattr__(child, name, getattr(scope, name))
            forged.plan.core.__dict__["object_scope"] = child
        elif fault == "scope-list":
            object.__setattr__(scope, "objects", list(scope.objects))
        elif fault == "scope-metric":
            object.__setattr__(scope.metrics, "object_count", True)
        else:
            entry = next(node.tree_entries[0] for node in scope.objects if node.tree_entries)
            object.__setattr__(entry, "name", entry.name.hex())
    reject(operation, forged)


def test_valid_model_construct_is_revalidated_not_treated_as_provenance(link):
    """构造途径不是认证凭据；有效声明可重建，篡改构造实例必须失败关闭。"""
    constructed = ProductGitPreparedLink.model_construct(**vars(link))
    assert snapshot_product_git_prepared_link(constructed, checkpoint=lambda: None) == link
    constructed.__dict__["approval"] = link.approval.model_copy(update={"plan_id": UUID(int=999)})
    reject(encode_product_git_prepared_link, constructed)


def test_construction_and_snapshot_detach_all_mutable_aliases(case, link):
    assert link.plan is not case.plan
    assert link.plan.core.call.arguments is not case.core.call.arguments
    snapshot = snapshot_product_git_prepared_link(link, checkpoint=lambda: None)
    assert snapshot is not link and snapshot == link
    assert snapshot.plan is not link.plan
    assert snapshot.approval is not link.approval
    assert snapshot.approval.diff_artifact is not link.approval.diff_artifact
    assert snapshot.plan.route.execution.workspace is not link.plan.route.execution.workspace
    assert (
        snapshot.plan.core.call.arguments["patches"]
        is not (link.plan.core.call.arguments["patches"])
    )
    snapshot.plan.core.call.arguments["patches"].append(str(UUID(int=999)))
    assert link.plan.core.call.arguments["patches"] == [str(UUID(int=10))]
    assert case.core.call.arguments["patches"] == [str(UUID(int=10))]


def test_pure_contract_and_codec_do_not_read_or_write_cas(case, link, monkeypatch):
    before = observe_state(case)
    expected = canonical(payload_for(link))

    def forbidden(*args, **kwargs):
        pytest.fail("纯声明合同及 codec 不得读取或写入材料、Session 或外部状态")

    for name in ("read", "persist"):
        monkeypatch.setattr(GitMaterialCAS, name, forbidden)
    for name in ("blob", "put_blob", "save", "transition"):
        monkeypatch.setattr(SQLiteWorkspaceTransactionStore, name, forbidden)
    assert encode_product_git_prepared_link(link, checkpoint=lambda: None) == expected
    assert decode_product_git_prepared_link(expected, checkpoint=lambda: None) == link
    assert observe_state(case) == before


def field_paths(payload, prefix=()):
    """逐个删除所有 JSON 对象字段，覆盖深层默认值、空值和完整对象范围。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield (*prefix, key)
            yield from field_paths(value, (*prefix, key))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from field_paths(value, (*prefix, index))


def test_no_wire_field_can_be_missing_then_default_completed(link):
    payload = payload_for(link)
    paths = tuple(field_paths(payload))
    assert len(paths) > 300
    for path in paths:
        changed = copy.deepcopy(payload)
        parent = changed
        for name in path[:-1]:
            parent = parent[name]
        del parent[path[-1]]
        reject(decode_product_git_prepared_link, canonical(changed))


@pytest.mark.parametrize(
    "mutation",
    [
        "leading-space",
        "trailing-newline",
        "pretty",
        "reordered",
        "bom",
        "invalid-utf8",
        "escaped-key",
        "escaped-unicode",
        "duplicate-root",
        "duplicate-nested",
        "nan",
        "infinity",
        "negative-infinity",
        "array",
        "null",
        "number",
        "trailing-record",
        "too-deep",
        "empty",
        "extra-root",
        "extra-approval",
        "extra-scope",
    ],
)
def test_noncanonical_duplicate_nonfinite_and_extra_wire_are_rejected(link, mutation):
    payload = payload_for(link)
    body = canonical(payload)
    if mutation == "leading-space":
        body = b" " + body
    elif mutation == "trailing-newline":
        body += b"\n"
    elif mutation == "pretty":
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
    elif mutation == "reordered":
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    elif mutation == "bom":
        body = b"\xef\xbb\xbf" + body
    elif mutation == "invalid-utf8":
        body = b"\xff" + body
    elif mutation == "escaped-key":
        body = body.replace(b'"approval"', b'"\\u0061pproval"', 1)
    elif mutation == "escaped-unicode":
        body = body.replace(b'"phase":"prepared"', b'"phase":"\\u0070repared"', 1)
        assert body != canonical(payload)
    elif mutation == "duplicate-root":
        body = b'{"phase":"prepared",' + body[1:]
    elif mutation == "duplicate-nested":
        body = body.replace(b'"decision":null', b'"decision":null,"decision":null', 1)
    elif mutation in {"nan", "infinity", "negative-infinity"}:
        constant = {"nan": b"NaN", "infinity": b"Infinity", "negative-infinity": b"-Infinity"}
        body = body.replace(b'"sequence":0', b'"sequence":' + constant[mutation], 1)
    elif mutation in {"array", "null", "number", "too-deep", "empty"}:
        body = {
            "array": b"[]",
            "null": b"null",
            "number": b"0",
            "too-deep": b"[" * 2048 + b"]" * 2048,
            "empty": b"",
        }[mutation]
    elif mutation == "trailing-record":
        body += b"{}"
    else:
        target = {
            "extra-root": payload,
            "extra-approval": payload["approval"],
            "extra-scope": payload["plan"]["core"]["object_scope"],
        }[mutation]
        target["undeclared"] = "private-sensitive"
        body = canonical(payload)
    reject(decode_product_git_prepared_link, body)


@pytest.mark.parametrize(
    "kind", ["str", "bytearray", "memoryview", "bytes-subclass", "dict", "none"]
)
def test_decoder_requires_exact_bytes_not_coercible_transport(link, kind):
    class ForeignBytes(bytes):
        pass

    body = canonical(payload_for(link))
    value = {
        "str": body.decode(),
        "bytearray": bytearray(body),
        "memoryview": memoryview(body),
        "bytes-subclass": ForeignBytes(body),
        "dict": payload_for(link),
        "none": None,
    }[kind]
    reject(decode_product_git_prepared_link, value)


@pytest.mark.parametrize("field", ["uuid", "date", "hex", "integer", "boolean", "revision"])
def test_strict_json_preserves_uuid_datetime_hex_and_scalar_semantics(link, field):
    payload = payload_for(link)
    if field == "uuid":
        payload["approval"]["approval_id"] = "not-a-uuid"
    elif field == "date":
        payload["approval"]["diff_artifact"]["expires_at"] = "not-a-date"
    elif field == "hex":
        objects = payload["plan"]["core"]["object_scope"]["objects"]
        next(node["tree_entries"][0] for node in objects if node["tree_entries"])["name"] = "0g"
    elif field == "integer":
        payload["approval"]["diff_artifact"]["records"] = "1"
    elif field == "boolean":
        payload["approval"]["diff_artifact"]["complete"] = 1
    else:
        payload["approval"]["request_fingerprint"] = "G" * 64
    reject(decode_product_git_prepared_link, canonical(payload))


@pytest.mark.parametrize("field", ["uuid", "date"])
def test_semantically_equivalent_uuid_date_spelling_is_not_canonical(link, field):
    payload = payload_for(link)
    if field == "uuid":
        original = payload["approval"]["approval_id"]
        payload["approval"]["approval_id"] = original.upper()
        assert original.upper() != original
    else:
        original = payload["approval"]["diff_artifact"]["expires_at"]
        assert original.endswith("Z")
        payload["approval"]["diff_artifact"]["expires_at"] = original[:-1] + "+00:00"
    reject(decode_product_git_prepared_link, canonical(payload))


@pytest.mark.parametrize("entry", ["snapshot", "encode", "decode"])
@pytest.mark.parametrize("stage", ["first", "inside", "last"])
@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        RuntimeError("parent-checkpoint"),
        ValueError("parent-checkpoint"),
        TypeError("parent-checkpoint"),
        AttributeError("parent-checkpoint"),
        RecursionError("parent-checkpoint"),
        KernelError("parent_cancel", "原检查点终止"),
        ValidationError.from_exception_data("parent-checkpoint", []),
    ],
)
def test_all_boundaries_preserve_exact_original_checkpoint_exception(link, entry, stage, failure):
    operation, value = {
        "snapshot": (snapshot_product_git_prepared_link, link),
        "encode": (encode_product_git_prepared_link, link),
        "decode": (decode_product_git_prepared_link, canonical(payload_for(link))),
    }[entry]
    total = 0

    def count():
        nonlocal total
        total += 1

    operation(value, checkpoint=count)
    assert total > 150
    at = {"first": 1, "inside": 150, "last": total}[stage]
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == at:
            raise failure

    with pytest.raises(type(failure)) as caught:
        operation(value, checkpoint=checkpoint)
    assert caught.value is failure and calls == at


def test_new_boundary_uses_original_snapshot_algorithm(link, monkeypatch):
    original = contracts_module._snapshot
    calls = []

    def snapshot(value, kind, checkpoint):
        calls.append(kind)
        return original(value, kind, checkpoint)

    monkeypatch.setattr(contracts_module, "_snapshot", snapshot)
    assert snapshot_product_git_prepared_link(link, checkpoint=lambda: None) == link
    assert calls == [
        ProductGitPreparedLink,
        ProductGitDeliveryPlanV2,
        TrustedActionApprovalRequestContent,
    ]


@pytest.mark.parametrize("entry", ["encode", "decode"])
def test_codec_consumes_public_snapshot_and_keeps_control_exception(link, monkeypatch, entry):
    failure = ValueError("public-snapshot-checkpoint")

    def snapshot(value, *, checkpoint):
        checkpoint()
        raise failure

    monkeypatch.setattr(wire_module, "snapshot_product_git_prepared_link", snapshot)
    operation, value = {
        "encode": (encode_product_git_prepared_link, link),
        "decode": (decode_product_git_prepared_link, canonical(payload_for(link))),
    }[entry]
    # 原控制身份只由 checkpoint 提供，任意被替换快照的 ValueError 仍是数据错误。
    if entry == "encode":
        with pytest.raises(ValueError) as caught:
            operation(value, checkpoint=lambda: None)
        assert caught.value is failure
    else:
        reject(operation, value)


def test_prepared_reuses_original_encoder_and_never_upgrades_old_plan(cas, tmp_path):
    case, legacy = make_observed_case(cas, tmp_path)
    link = link_for(case)
    assert wire_module._encode is (
        __import__("harnessix.product_config.git_delivery_plan_wire", fromlist=["_encode"])._encode
    )
    assert wire_module.MAX_PRODUCT_GIT_PLAN_BYTES == MAX_PRODUCT_GIT_PLAN_BYTES == 512 * 1024
    new_body = canonical(payload_for(link))
    for operation in (decode_product_git_delivery_plan, decode_product_git_delivery_plan_v2):
        reject(operation, new_body)
    for plan, encoder, decoder in (
        (legacy.plan, encode_product_git_delivery_plan, decode_product_git_delivery_plan),
        (case.plan, encode_product_git_delivery_plan_v2, decode_product_git_delivery_plan_v2),
    ):
        body = encoder(plan, checkpoint=lambda: None)
        assert decoder(body, checkpoint=lambda: None) == plan
        reject(decode_product_git_prepared_link, body)
        reject(encode_product_git_prepared_link, plan)
    reject(encode_product_git_prepared_link, link.model_copy(update={"plan": legacy.plan}))


def test_exact_512k_prepared_body_passes_but_one_more_byte_fails(cas, tmp_path):
    """真实合法 Commit 凑齐 UTF-8 字节边界，全部字段不裁剪，预算不提高。"""

    def sized(path, message, provider="p"):
        case, _legacy = make_observed_case(
            cas, path, action="commit", commit_message=message, provider_call_id=provider
        )
        link = link_for(case)
        return link, canonical(payload_for(link))

    message = "a" * 65535 + "\n"
    _small, body = sized(tmp_path / "size-0000", message)
    assert body.count(canonical(message)) == 4
    extra = (MAX_PRODUCT_GIT_PLAN_BYTES - len(body) - 64) // 4
    emojis, accents = divmod(extra, 3)
    assert 0 <= emojis + accents <= 65535
    message = "🙂" * emojis + "é" * accents + "a" * (65535 - emojis - accents) + "\n"
    _near, body = sized(tmp_path / "size-0001", message)
    padding = MAX_PRODUCT_GIT_PLAN_BYTES - len(body)
    assert 0 <= padding <= 254
    exact, body = sized(tmp_path / "size-0002", message, "p" * (padding + 1))
    assert len(body) == MAX_PRODUCT_GIT_PLAN_BYTES
    assert encode_product_git_prepared_link(exact, checkpoint=lambda: None) == body
    assert decode_product_git_prepared_link(body, checkpoint=lambda: None) == exact
    over, body = sized(tmp_path / "size-0003", message, "p" * (padding + 2))
    assert len(body) == MAX_PRODUCT_GIT_PLAN_BYTES + 1
    reject(encode_product_git_prepared_link, over)
    reject(decode_product_git_prepared_link, body)


def test_full_parent_history_survives_prepared_wire(cas, tmp_path):
    case, _legacy = make_observed_case(cas, tmp_path, depth=64)
    link = link_for(case)
    before = observe_state(case)
    restored = decode_product_git_prepared_link(
        canonical(payload_for(link)), checkpoint=lambda: None
    )
    assert len(case.history) == 65
    assert restored.plan.core.baseline.source.workspace == case.core.baseline.source.workspace
    assert restored.plan.core.object_scope.external_history.parents == (
        case.core.object_scope.external_history.parents
    )
    assert observe_state(case) == before


def test_independent_observation_is_not_lost_when_copied_from_existing_model(case, link):
    altered = case.plan.model_copy(deep=True)
    altered.core.user_observation.__dict__["fingerprint"] = "0" * 64
    with pytest.raises((KernelError, ValidationError)):
        ProductGitPreparedLink(plan=altered, approval=link.approval)
