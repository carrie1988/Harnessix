"""Git 产品计划的完整合同、单向指纹与有界规范 Wire；不证明业务批准。"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import PolicyDecisionKind
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_delivery_plan_contracts import (
    GitIndexFileObservation,
    ProductGitCheckpointInput,
    ProductGitCommitInput,
    ProductGitDeliveryCore,
    ProductGitDeliveryPlan,
    ProductGitWorktreeIntent,
    product_git_delivery_resource,
)
from harnessix.product_config.git_delivery_plan_snapshot import (
    snapshot_product_git_delivery_core,
    snapshot_product_git_delivery_plan,
)
from harnessix.product_config.git_delivery_plan_wire import (
    decode_product_git_delivery_plan,
    encode_product_git_delivery_plan,
)
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from tests.product_config.git_delivery_plan_support import (
    NOW,
    ZERO,
    canonical_bytes,
    make_case,
    observe_state,
    rehash,
    route_for,
    sealed,
)


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


def reject(body, *, checkpoint=lambda: None):
    """拒绝时只返回正式固定领域错误，不泄露路径或正文。"""
    with pytest.raises(KernelError) as caught:
        decode_product_git_delivery_plan(body, checkpoint=checkpoint)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert caught.value.__cause__ is None
    assert "新代码" not in str(caught.value)


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_complete_contract_and_wire_roundtrip(cas, tmp_path, fmt, action, platform):
    """两格式、动作和平台逻辑元数据全字段往返；不声称原生 Windows 执行。"""
    case = make_case(cas, tmp_path, fmt, action, platform)
    before = observe_state(case)
    body = encode_product_git_delivery_plan(case.plan, checkpoint=lambda: None)
    assert body == canonical_bytes(case.plan.model_dump(mode="json", warnings="error"))
    result = decode_product_git_delivery_plan(body, checkpoint=lambda: None)
    assert result == case.plan and result is not case.plan
    assert result.core is not case.core and result.core.object_scope is not case.core.object_scope
    assert (
        result.core.baseline.source.workspace.parent_closure
        == case.core.baseline.source.workspace.parent_closure
    )
    assert (
        result.core.object_scope.external_history.parents[0]
        == result.core.object_scope.external_history.parents[2]
    )
    assert result.core.diff_sha256 != result.review_artifact.sha256
    assert observe_state(case) == before
    assert not hasattr(result, "approved") and not hasattr(result, "authorize")


def test_full_parent_history_reference_survives_exact_roundtrip(cas, tmp_path):
    """原 CAS 的全部父节点及 Manifest/Chunk 引用不得裁剪为叶资源。"""
    case = make_case(cas, tmp_path, depth=64)
    snapshot = case.core.baseline.source.workspace
    manifest = json.loads(cas.store.blob(snapshot.parent_closure.sha256))
    assert len(case.history) == len(manifest["nodes"]) == 65
    old_chunks = tuple(
        (row["sha256"], row["size"], cas.store.blob(row["sha256"])) for row in manifest["chunks"]
    )
    result = decode_product_git_delivery_plan(
        encode_product_git_delivery_plan(case.plan, checkpoint=lambda: None),
        checkpoint=lambda: None,
    )
    restored = result.core.baseline.source.workspace
    assert restored == snapshot
    assert (
        read_workspace_parent_closure(restored, cas.store.blob, checkpoint=lambda: None)
        == case.history
    )
    assert (
        tuple(
            (row["sha256"], row["size"], cas.store.blob(row["sha256"]))
            for row in manifest["chunks"]
        )
        == old_chunks
    )


def test_resource_uses_core_not_envelope_fingerprint(cas, tmp_path):
    """Core→资源→Router→封套为单向依赖，无循环计划摘要。"""
    case = make_case(cas, tmp_path)
    resource = product_git_delivery_resource(case.core)
    core = case.core
    assert resource.kind == "external" and resource.access == "write"
    assert resource.identifier_sha256 == canonical_digest(
        {
            name: str(getattr(core, name))
            for name in ("store_id", "key_id", "delivery_id", "thread_id", "turn_id")
        }
        | {"call_id": str(core.call.call_id)}
    )
    assert resource.attributes_sha256 == core.fingerprint
    assert resource.attributes_sha256 != case.plan.fingerprint
    assert core.fingerprint == canonical_digest(
        core.model_dump(mode="json", exclude={"fingerprint"})
    )
    assert case.plan.fingerprint == canonical_digest(
        case.plan.model_dump(mode="json", exclude={"fingerprint"})
    )


@pytest.mark.parametrize("count", [1, 256])
def test_checkpoint_input_exact_selected_uuid_limits(count):
    value = ProductGitCheckpointInput(patches=tuple(UUID(int=n + 1) for n in range(count)))
    assert ProductGitCheckpointInput.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "patches",
    [
        (),
        (UUID(int=1), UUID(int=1)),
        tuple(UUID(int=n + 1) for n in range(257)),
        ("not-uuid",),
        (1,),
        [UUID(int=1)],
    ],
)
def test_checkpoint_input_refuses_empty_duplicate_overflow_or_coercion(patches):
    with pytest.raises(ValidationError):
        ProductGitCheckpointInput(patches=patches)


@pytest.mark.parametrize(
    "field,value",
    [
        ("checkpoint_id", "invalid"),
        ("branch_ref", "refs/heads/a/../b"),
        ("branch_ref", "refs/tags/tag"),
        ("branch_ref", "refs/heads/a.lock/x"),
        ("author_name", "A\nB"),
        ("author_email", "a b@example.invalid"),
        ("message", "nul\0\n"),
        ("message", "no final LF"),
        ("authored_at", "2026-10-07T08:09:10"),
        ("path", "/arbitrary"),
        ("executable", "/bin/git"),
        ("environment", {"A": "B"}),
        ("env", {"GIT_CONFIG_COUNT": "1"}),
        ("script", "/arbitrary/script.sh"),
        ("command", ["/bin/sh", "-c", "arbitrary"]),
        ("git_arguments", ["push", "--force"]),
    ],
)
def test_commit_input_finite_full_fields(field, value):
    payload = dict(
        checkpoint_id=UUID(int=1),
        branch_ref="refs/heads/new",
        author_name="Author",
        author_email="a@example.invalid",
        message="message\n",
        authored_at=NOW,
    )
    payload[field] = value
    with pytest.raises(ValidationError):
        ProductGitCommitInput(**payload)


@pytest.mark.parametrize(
    "presence,identity,digest,size",
    [
        ("absent", None, None, 0),
        ("file", "1" * 64, "2" * 64, 0),
        ("file", "1" * 64, "2" * 64, 8 * 1024 * 1024),
    ],
)
def test_physical_index_observation_valid_pairs(presence, identity, digest, size):
    value = GitIndexFileObservation(presence=presence, identity=identity, sha256=digest, size=size)
    assert GitIndexFileObservation.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "updates",
    [
        {"presence": "absent", "size": 1},
        {"presence": "absent", "identity": "1" * 64},
        {"presence": "absent", "sha256": "1" * 64},
        {"presence": "file"},
        {"presence": "file", "identity": "1" * 64},
        {"presence": "file", "sha256": "1" * 64},
        {"size": True},
        {"size": -1},
        {"size": 8 * 1024 * 1024 + 1},
        {"presence": "symlink"},
        {"identity": "upper"},
        {"extra": 1},
    ],
)
def test_physical_index_observation_invalid_pairs(updates):
    payload = dict(presence="absent", identity=None, sha256=None, size=0)
    payload.update(updates)
    with pytest.raises(ValidationError):
        GitIndexFileObservation(**payload)


@pytest.mark.parametrize(
    "platform,parent", [("posix", "/private/worktrees"), ("windows", r"C:\Harnessix\worktrees")]
)
def test_worktree_path_is_derived_and_expected_missing(platform, parent):
    intent = ProductGitWorktreeIntent(
        role="anchor",
        worktree_id=UUID(int=1),
        parent_path=parent,
        parent_identity="a" * 64,
        platform=platform,
        base_commit_oid="b" * 40,
    )
    assert str(intent.worktree_id) in intent.path
    assert intent.expected_missing is True
    assert ProductGitWorktreeIntent.model_validate_json(intent.model_dump_json()) == intent


@pytest.mark.parametrize(
    "updates",
    [
        {"parent_path": "relative"},
        {"parent_path": "/private\0/path"},
        {"parent_path": "/private\n/path"},
        {"role": "target"},
        {"worktree_id": "bad"},
        {"parent_identity": "bad"},
        {"base_commit_oid": "b" * 39},
        {"expected_missing": False},
        {"path": "/override"},
        {"platform": "linux"},
    ],
)
def test_worktree_intent_refuses_free_paths_and_invalid_metadata(updates):
    payload = dict(
        role="anchor",
        worktree_id=UUID(int=1),
        parent_path="/private/worktrees",
        parent_identity="a" * 64,
        platform="posix",
        base_commit_oid="b" * 40,
    )
    payload.update(updates)
    with pytest.raises(ValidationError):
        ProductGitWorktreeIntent(**payload)


@pytest.mark.parametrize(
    "field",
    [
        "delivery_id",
        "store_id",
        "key_id",
        "thread_id",
        "turn_id",
        "call",
        "baseline",
        "common_directory_path_sha256",
        "common_directory_identity",
        "index_file_observation",
        "anchor_intent",
        "worktree_intent",
        "checkpoint_delivery_id",
        "object_scope",
        "diff_sha256",
        "diff_bytes",
        "implementation_digest",
        "commit_spec",
        "fingerprint",
    ],
)
def test_every_core_field_is_envelope_bound(cas, tmp_path, field):
    case = make_case(cas, tmp_path)
    payload = case.plan.model_dump(mode="json")
    value = payload["core"][field]
    payload["core"][field] = (
        value + 1 if type(value) is int else ZERO if type(value) is str else {"forged": True}
    )
    reject(canonical_bytes(payload))


@pytest.mark.parametrize(
    "fault",
    [
        "thread",
        "readonly",
        "approval",
        "source-patches",
        "same-worktree",
        "base-oid",
        "action",
        "checkpoint-delivery",
        "commit-spec",
    ],
)
def test_core_semantic_mismatch_rejected_even_after_rehash(cas, tmp_path, fault):
    case = make_case(cas, tmp_path)
    core = case.core.model_dump(mode="json")
    if fault == "thread":
        core["thread_id"] = str(UUID(int=99))
    elif fault == "readonly":
        core["call"]["effect_class"] = "read_only"
    elif fault == "approval":
        core["call"]["requires_approval"] = False
    elif fault == "source-patches":
        core["call"]["arguments"]["patches"] = [str(UUID(int=99))]
    elif fault == "same-worktree":
        core["worktree_intent"]["worktree_id"] = core["anchor_intent"]["worktree_id"]
    elif fault == "base-oid":
        core["anchor_intent"]["base_commit_oid"] = "f" * len(
            core["anchor_intent"]["base_commit_oid"]
        )
    elif fault == "action":
        core["object_scope"]["action_kind"] = "commit"
    elif fault == "checkpoint-delivery":
        core["checkpoint_delivery_id"] = str(UUID(int=99))
    else:
        core["commit_spec"] = {}
    with pytest.raises((ValidationError, KernelError)):
        ProductGitDeliveryCore.model_validate_json(canonical_bytes(rehash(core)), strict=True)


@pytest.mark.parametrize("decision", [PolicyDecisionKind.ALLOW, PolicyDecisionKind.DENY])
def test_fully_valid_original_route_cannot_bypass_independent_approval(cas, tmp_path, decision):
    case = make_case(cas, tmp_path)
    route = route_for(case.core, decision)
    with pytest.raises((ValidationError, KernelError)):
        sealed(
            ProductGitDeliveryPlan,
            dict(core=case.core, route=route, review_artifact=case.plan.review_artifact),
        )


@pytest.mark.parametrize(
    "fault",
    [
        "missing-resource",
        "read-resource",
        "wrong-core-resource",
        "external-id",
        "invocation-id",
        "artifact-incomplete",
        "artifact-extra",
    ],
)
def test_envelope_semantic_mismatch_after_nested_rehash(cas, tmp_path, fault):
    case = make_case(cas, tmp_path)
    payload = case.plan.model_dump(mode="json")
    route = payload["route"]
    if fault in {"missing-resource", "read-resource", "wrong-core-resource"}:
        if fault == "missing-resource":
            route["resources"] = []
        else:
            route["resources"][0]["access" if fault == "read-resource" else "attributes_sha256"] = (
                "read" if fault == "read-resource" else ZERO
            )
        route["resources_sha256"] = canonical_digest(
            [
                (r["kind"], r["access"], r["identifier_sha256"], r["attributes_sha256"])
                for r in route["resources"]
            ]
        )
    elif fault == "external-id":
        route["external_action_id"] = str(UUID(int=99))
    elif fault == "invocation-id":
        route["invocation"]["invocation_id"] = str(UUID(int=99))
        route["execution"]["plan_id"] = str(UUID(int=99))
        rehash(route["execution"])
    elif fault == "artifact-incomplete":
        payload["review_artifact"]["complete"] = False
    else:
        payload["review_artifact"]["body"] = "not allowed"
    rehash(route)
    reject(canonical_bytes(rehash(payload)))


@pytest.mark.parametrize(
    "mutation",
    [
        "space",
        "newline",
        "bom",
        "duplicate",
        "reordered",
        "escaped-key",
        "invalid-utf8",
        "nan",
        "array",
        "oversize",
        "extra",
    ],
)
def test_wire_refuses_noncanonical_alias_duplicate_or_unbounded_bodies(cas, tmp_path, mutation):
    case = make_case(cas, tmp_path)
    body = canonical_bytes(case.plan.model_dump(mode="json"))
    if mutation == "space":
        body = b" " + body
    elif mutation == "newline":
        body += b"\n"
    elif mutation == "bom":
        body = b"\xef\xbb\xbf" + body
    elif mutation == "duplicate":
        body = b'{"fingerprint":"' + case.plan.fingerprint.encode() + b'",' + body[1:]
    elif mutation == "reordered":
        body = json.dumps(json.loads(body), sort_keys=False, indent=1).encode()
    elif mutation == "escaped-key":
        body = body.replace(b'"fingerprint"', b'"\\u0066ingerprint"', 1)
    elif mutation == "invalid-utf8":
        body = b"\xff" + body
    elif mutation == "nan":
        body = b'{"fingerprint":NaN}'
    elif mutation == "array":
        body = b"[]"
    elif mutation == "oversize":
        body = b" " * (512 * 1024 + 1)
    else:
        body = canonical_bytes({**case.plan.model_dump(mode="json"), "extra": 1})
    reject(body)


@pytest.mark.parametrize("fault", ["fingerprint", "extra", "nested-type", "scope-extra"])
def test_encoder_rebuilds_forged_model_instances(cas, tmp_path, fault):
    case = make_case(cas, tmp_path)
    forged = case.plan.model_copy(deep=True)
    if fault == "fingerprint":
        forged = forged.model_copy(update={"fingerprint": ZERO})
    elif fault == "extra":
        forged.__dict__["undeclared"] = "hidden"
    elif fault == "nested-type":
        forged.core.baseline.source.workspace.__dict__["parent_closure"] = {}
    else:
        object.__setattr__(forged.core.object_scope, "objects", [])
    with pytest.raises(KernelError) as caught:
        encode_product_git_delivery_plan(forged, checkpoint=lambda: None)
    assert caught.value.code == "git_delivery_plan_invalid"


@pytest.mark.parametrize("entry", ["encode", "decode", "core-snapshot", "plan-snapshot"])
@pytest.mark.parametrize("at", [1, 150])
@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        RuntimeError("parent-checkpoint"),
        ValueError("parent-checkpoint"),
        TypeError("parent-checkpoint"),
        KernelError("parent_cancel", "原检查点终止"),
    ],
)
def test_wire_preserves_original_control_exception(cas, tmp_path, entry, at, failure):
    case = make_case(cas, tmp_path)
    ports = {
        "encode": (encode_product_git_delivery_plan, case.plan),
        "decode": (
            decode_product_git_delivery_plan,
            canonical_bytes(case.plan.model_dump(mode="json")),
        ),
        "core-snapshot": (snapshot_product_git_delivery_core, case.core),
        "plan-snapshot": (snapshot_product_git_delivery_plan, case.plan),
    }
    function, value = ports[entry]
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == at:
            raise failure

    with pytest.raises(type(failure)) as caught:
        function(value, checkpoint=checkpoint)
    assert caught.value is failure
    assert calls == at


def test_explicit_deep_snapshot_detaches_arguments_and_every_nested_contract(cas, tmp_path):
    case = make_case(cas, tmp_path)
    core = snapshot_product_git_delivery_core(case.core, checkpoint=lambda: None)
    plan = snapshot_product_git_delivery_plan(case.plan, checkpoint=lambda: None)
    assert core == case.core and plan == case.plan
    assert core.call.arguments is not case.core.call.arguments
    assert core.call.arguments["patches"] is not case.core.call.arguments["patches"]
    assert plan.route.execution.workspace is not case.plan.route.execution.workspace
    assert (
        plan.core.object_scope.objects[0].material is not case.core.object_scope.objects[0].material
    )
    core.call.arguments["patches"].append(str(UUID(int=99)))
    assert case.core.call.arguments["patches"] == [str(UUID(int=10))]


def test_encoder_retains_original_512kib_byte_cap_without_truncating_valid_model(cas, tmp_path):
    """字符合同有效的 UTF-8 长消息可使完整封套超限；不得裁剪字段或提高上限。"""
    case = make_case(cas, tmp_path, action="commit", commit_message="🙂" * 40000 + "\n")
    body = canonical_bytes(case.plan.model_dump(mode="json"))
    assert len(body) > 512 * 1024
    assert len(case.core.commit_spec.message) <= 65536
    before = observe_state(case)
    with pytest.raises(KernelError) as caught:
        encode_product_git_delivery_plan(case.plan, checkpoint=lambda: None)
    assert caught.value.code == "git_delivery_plan_invalid"
    reject(body)
    assert observe_state(case) == before


@pytest.mark.parametrize(
    "fault",
    [
        "anchor",
        "delivery",
        "checkpoint-delivery",
        "selected-checkpoint",
        "implementation",
        "base-tree",
    ],
)
def test_commit_core_pairing_refuses_semantic_changes_after_rehash(cas, tmp_path, fault):
    case = make_case(cas, tmp_path, action="commit")
    payload = case.core.model_dump(mode="json")
    if fault in {"anchor", "delivery"}:
        checkpoint_case = make_case(cas, tmp_path / "separate")
        payload["anchor_intent" if fault == "anchor" else "worktree_intent"] = (
            checkpoint_case.core.anchor_intent.model_dump(mode="json")
        )
    elif fault == "checkpoint-delivery":
        payload["checkpoint_delivery_id"] = None
    elif fault == "selected-checkpoint":
        payload["call"]["arguments"]["checkpoint_id"] = str(UUID(int=99))
    elif fault == "implementation":
        payload["implementation_digest"] = ZERO
    else:
        payload["commit_spec"]["checkpoint"]["base_tree_oid"] = "f" * 64
        rehash(payload["commit_spec"]["checkpoint"], "digest")
        rehash(payload["commit_spec"])
    with pytest.raises((ValidationError, KernelError)):
        ProductGitDeliveryCore.model_validate_json(canonical_bytes(rehash(payload)), strict=True)


@pytest.mark.parametrize(
    "parent", [r"C:relative", r"\rooted", r"C:\a\..\b", r"C:\a\.\b", "C:/a/b", "C:\\a\n\\b"]
)
def test_windows_worktree_intent_requires_canonical_drive_absolute_parent(parent):
    """只验证 Windows 路径声明，不调用或模拟 Windows Native API。"""
    with pytest.raises(ValidationError):
        ProductGitWorktreeIntent(
            role="delivery",
            worktree_id=UUID(int=1),
            parent_path=parent,
            parent_identity="a" * 64,
            platform="windows",
            base_commit_oid="b" * 64,
        )
