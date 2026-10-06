"""完整 U 观察的 Core2/Plan2 合同与真实原 CAS；不等于 Session 或批准认证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import fields, replace
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_inventory_contracts import GitInventoryScope
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import PolicyDecisionKind
from harnessix.product_config import git_delivery_core_store as store_module
from harnessix.product_config import git_delivery_observed_wire as observed_wire_module
from harnessix.product_config import git_delivery_plan_materials as materials
from harnessix.product_config import git_delivery_plan_snapshot as snapshot_module
from harnessix.product_config import git_delivery_plan_wire as wire_module
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
from harnessix.product_config.git_delivery_observed_wire import (
    decode_product_git_delivery_core_v2,
    decode_product_git_delivery_plan_v2,
    encode_product_git_delivery_core_v2,
    encode_product_git_delivery_plan_v2,
    snapshot_product_git_delivery_core_v2,
    snapshot_product_git_delivery_plan_v2,
)
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitDeliveryCore,
    ProductGitDeliveryPlan,
    product_git_delivery_core_fingerprint,
    product_git_delivery_plan_fingerprint,
    product_git_delivery_resource,
)
from harnessix.product_config.git_delivery_plan_wire import (
    MAX_PRODUCT_GIT_PLAN_BYTES,
    decode_product_git_delivery_core,
    decode_product_git_delivery_plan,
    encode_product_git_delivery_core,
    encode_product_git_delivery_plan,
)
from harnessix.product_config.git_delivery_route_core import (
    load_product_git_delivery_route_core,
    load_product_git_delivery_route_core_v2,
)
from harnessix.trusted_actions.planning import external_action_identity
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure
from tests.product_config.git_delivery_plan_support import observe_state, route_for
from tests.support.git_delivery_observed_core import (
    at,
    canonical,
    core_bytes,
    core_payload,
    json_facts,
    make_observed_case,
    model_fields,
    plan_for,
    plan_payload,
    rebind_identity,
    rehash,
    reseal_core_payload,
    seal,
)


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


@pytest.fixture
def case(cas, tmp_path):
    return make_observed_case(cas, tmp_path)[0]


def reject(operation, *args, code="git_delivery_plan_invalid"):
    """数据拒绝只允许固定公开错误，原控制异常另行核验身份。"""
    with pytest.raises(KernelError) as caught:
        operation(*args, checkpoint=lambda: None)
    assert caught.value.code == code
    assert caught.value.__cause__ is None
    assert "新代码" not in str(caught.value)
    assert "private-sensitive" not in str(caught.value)
    return caught.value


def readonly(case, operation, *args):
    before = observe_state(case)
    try:
        return operation(*args, checkpoint=lambda: None)
    finally:
        assert observe_state(case) == before


def rebuild(core, **updates):
    return seal(ProductGitDeliveryCoreV2, {**model_fields(core), **updates})


def resource_route(route, resources, **updates):
    """只修改测试路由声明；摘要来自独立预期，不替代认证读取。"""
    identities = [
        [resource.kind, resource.access, resource.identifier_sha256, resource.attributes_sha256]
        for resource in resources
    ]
    return seal(
        ActionRoutePlanV2,
        {
            **model_fields(route),
            "resources": resources,
            "resources_sha256": hashlib.sha256(canonical(identities)).hexdigest(),
            **updates,
        },
    )


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
def test_first_real_cas_complete_observation_roundtrip(cas, tmp_path, fmt, action):
    """规范字节正例使用全材料 CAS，预期不取自任何产品编码或摘要函数。"""
    case, _legacy = make_observed_case(cas, tmp_path, fmt=fmt, action=action)
    expected = core_bytes(case.core)
    assert hashlib.sha256(expected).hexdigest() == case.core.fingerprint
    assert encode_product_git_delivery_core_v2(case.core, checkpoint=lambda: None) == expected
    restored = decode_product_git_delivery_core_v2(expected, checkpoint=lambda: None)
    assert type(restored) is ProductGitDeliveryCoreV2
    assert not isinstance(restored, ProductGitDeliveryCore)
    assert restored == case.core and restored is not case.core
    assert restored.user_observation is not case.core.user_observation
    assert restored.baseline is restored.user_observation.baseline
    assert restored.object_scope is not case.core.object_scope
    assert encode_product_git_delivery_plan_v2(case.plan, checkpoint=lambda: None) == canonical(
        plan_payload(case.plan)
    )
    envelope = decode_product_git_delivery_plan_v2(
        canonical(plan_payload(case.plan)), checkpoint=lambda: None
    )
    assert type(envelope) is ProductGitDeliveryPlanV2 and envelope == case.plan
    for material in case.bodies:
        assert (
            cas.read(
                next(
                    node.material
                    for node in restored.object_scope.objects
                    if node.material.object_id == material.object_id
                )
            ).body
            == material.body
        )


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_cas_route_materials_preserve_full_diff_once(
    cas, tmp_path, monkeypatch, fmt, action, platform
):
    """Windows 只验证声明；材料核验只读且一次返回原完整 Diff，不冒充原生验收。"""
    case, _legacy = make_observed_case(cas, tmp_path, fmt=fmt, action=action, platform=platform)
    owner = ProductGitDeliveryCoreStore(cas.store)
    before_rows = tuple(cas.store._db.iterdump())
    before_blobs = {path.name for path in cas.store._blobs.iterdir()}
    saved = owner.persist_v2(case.core, checkpoint=lambda: None)
    assert saved == case.core and saved is not case.core
    assert cas.store.blob(saved.fingerprint) == core_bytes(case.core)
    assert tuple(cas.store._db.iterdump()) == before_rows
    assert {path.name for path in cas.store._blobs.iterdir()} - before_blobs == {saved.fingerprint}
    assert readonly(case, owner.load_v2, saved.fingerprint) == saved
    assert readonly(case, load_product_git_delivery_route_core_v2, owner, case.plan.route) == saved
    reads = Counter()
    calls = Counter()
    original_read = GitMaterialCAS.read
    original_prepare = materials.prepare_git_tree_diff

    def read(actual, reference):
        reads[reference.object_id] += 1
        return original_read(actual, reference)

    def prepare(*args, **kwargs):
        calls["diff"] += 1
        return original_prepare(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("只读材料与 Route 恢复不得写 CAS、工作区或业务记录")

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
    monkeypatch.setattr(materials, "prepare_git_tree_diff", prepare)
    for name in ("put_blob", "_put_blob", "save", "transition"):
        monkeypatch.setattr(SQLiteWorkspaceTransactionStore, name, forbidden)
    result = readonly(case, materials.read_product_git_delivery_core_materials_v2, cas, saved)
    assert type(result) is materials.VerifiedProductGitDeliveryMaterials
    assert type(result.core) is ProductGitDeliveryCoreV2 and result.core == saved
    assert result.core is not saved
    assert calls["diff"] == 1
    content = result.diff.content
    path = saved.baseline.source.mutations[0].path
    expected = (
        f"diff --harnessix a/{path} b/{path}\n"
        "old mode 644\nnew mode 755\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+新代码🙂\n"
    )
    assert content.text == expected == case.diff_text
    assert content.utf8_bytes == len(expected.encode()) == saved.diff_bytes
    assert content.sha256 == hashlib.sha256(expected.encode()).hexdigest() == saved.diff_sha256
    assert content.sha256 != case.plan.review_artifact.sha256
    assert len(content.entries) == 1
    entry = content.entries[0]
    mutation = saved.baseline.source.mutations[0]
    assert (entry.kind, entry.path, entry.original_path, entry.binary) == (
        "modified",
        path,
        None,
        False,
    )
    assert entry.before == mutation.before and entry.after == mutation.after
    assert result.diff.projection.root.object_id == saved.object_scope.roots.target_tree.object_id
    assert {file.path for file in result.diff.projection.files} == {path, "keep"}
    assert set(reads) == {node.material.object_id for node in saved.object_scope.objects}
    assert not set(reads) & set(saved.object_scope.external_history.unique_parent_ids)
    verified = readonly(case, materials.verify_product_git_delivery_core_materials_v2, cas, saved)
    assert type(verified) is ProductGitDeliveryCoreV2 and verified == saved
    assert calls["diff"] == 2
    assert "新代码" not in repr(result)
    assert not (tmp_path / "worktrees").exists()
    assert not any(hasattr(verified, name) for name in ("approved", "mac_verified", "authorized"))


@pytest.mark.parametrize("action", ["checkpoint", "commit"])
def test_writer_closed_reopens_readonly_without_recovery_writes(cas, tmp_path, action):
    case, _legacy = make_observed_case(cas, tmp_path, action=action)
    owner = ProductGitDeliveryCoreStore(cas.store)
    saved = owner.persist_v2(case.core, checkpoint=lambda: None)
    assert owner.persist_v2(case.core, checkpoint=lambda: None) == saved
    root = cas.store._root
    cas.store.close()
    with SQLiteWorkspaceTransactionStore(root, read_only=True) as reader:
        reopened = replace(case, cas=GitMaterialCAS(reader))
        readonly_owner = ProductGitDeliveryCoreStore(reader)
        assert readonly(reopened, readonly_owner.load_v2, saved.fingerprint) == saved
        assert (
            readonly(
                reopened, load_product_git_delivery_route_core_v2, readonly_owner, case.plan.route
            )
            == saved
        )
        verified = readonly(
            reopened, materials.read_product_git_delivery_core_materials_v2, reopened.cas, saved
        )
        assert verified.diff.content.text == case.diff_text
        reject(readonly_owner.persist_v2, saved, code="git_delivery_core_write_failed")
        assert reader.blob(saved.fingerprint) == core_bytes(saved)


def test_all_parent_references_full_source_scope_hex_and_no_legacy_aliases(cas, tmp_path):
    case, _legacy = make_observed_case(cas, tmp_path, depth=64)
    core = case.core
    owner = ProductGitDeliveryCoreStore(cas.store)
    source = core.baseline.source
    snapshot = source.workspace
    manifest_body = cas.store.blob(snapshot.parent_closure.sha256)
    manifest = json.loads(manifest_body)
    chunks = tuple((row, cas.store.blob(row["sha256"])) for row in manifest["chunks"])
    assert len(case.history) == len(manifest["nodes"]) == 65
    saved = owner.persist_v2(core, checkpoint=lambda: None)
    loaded = readonly(case, owner.load_v2, saved.fingerprint)
    assert loaded.baseline.source == source
    assert loaded.object_scope == core.object_scope
    assert (
        loaded.object_scope.external_history.parents[0]
        == loaded.object_scope.external_history.parents[2]
    )
    assert loaded.baseline.source.workspace.parent_closure == snapshot.parent_closure
    assert (
        read_workspace_parent_closure(
            loaded.baseline.source.workspace, cas.store.blob, checkpoint=lambda: None
        )
        == case.history
    )
    assert cas.store.blob(snapshot.parent_closure.sha256) == manifest_body
    assert tuple((row, cas.store.blob(row["sha256"])) for row in manifest["chunks"]) == chunks
    payload = json.loads(core_bytes(loaded))
    old_names = {
        "baseline",
        "common_directory_path_sha256",
        "common_directory_identity",
        "index_file_observation",
    }
    assert not old_names & payload.keys()
    assert core_bytes(loaded).count(b'"baseline":') == 1
    assert payload["user_observation"]["baseline"]["source"] == json_facts(source)
    assert payload["object_scope"] == json_facts(core.object_scope)
    assert loaded.index_file_observation is loaded.user_observation.index_file_observation
    assert loaded.common_directory_identity == loaded.user_observation.common_directory_identity
    assert (
        loaded.common_directory_path_sha256 == loaded.user_observation.common_directory_path_sha256
    )
    for node in payload["object_scope"]["objects"]:
        for entry in node["tree_entries"]:
            assert entry["name"] == bytes.fromhex(entry["name"]).hex()
            assert "name_hex" not in entry
    for name in old_names:
        with pytest.raises((AttributeError, ValueError)):
            setattr(loaded, name, getattr(core, name))
    schema = ProductGitDeliveryCoreV2.model_json_schema()
    assert not old_names & schema["properties"].keys()
    assert "user_observation" in schema["properties"]
    assert schema["additionalProperties"] is False
    entry_schema = schema["$defs"]["GitTreeEntry"]
    assert entry_schema["properties"]["name"]["format"] == "hex"


def test_original_v1_bytes_and_frozen_baseline_schemas_are_unchanged(cas, tmp_path):
    case, legacy = make_observed_case(cas, tmp_path)
    expected_core = canonical(
        {k: v for k, v in json_facts(legacy.core).items() if k != "fingerprint"}
    )
    expected_plan = canonical(json_facts(legacy.plan))
    assert encode_product_git_delivery_core(legacy.core, checkpoint=lambda: None) == expected_core
    assert encode_product_git_delivery_plan(legacy.plan, checkpoint=lambda: None) == expected_plan
    assert (
        type(decode_product_git_delivery_core(expected_core, checkpoint=lambda: None))
        is ProductGitDeliveryCore
    )
    assert (
        type(decode_product_git_delivery_plan(expected_plan, checkpoint=lambda: None))
        is ProductGitDeliveryPlan
    )
    # 固定值来自 d852474 原合同，不从受测源码的 Schema 自证兼容。
    golden = {
        ProductGitDeliveryCore: "2a659dde2e214c31c6c06b382571ccc5599393f59c8a5011b869851da822520b",
        ProductGitDeliveryPlan: "db89d3b9e8fa091d5ad3d17669c0e75a8a8233ee3fc967aca5349e21ee7d0f0e",
    }
    for kind, digest in golden.items():
        assert hashlib.sha256(canonical(kind.model_json_schema())).hexdigest() == digest
    reject(decode_product_git_delivery_core_v2, expected_core)
    reject(decode_product_git_delivery_plan_v2, expected_plan)
    reject(decode_product_git_delivery_core, core_bytes(case.core))
    reject(decode_product_git_delivery_plan, canonical(plan_payload(case.plan)))
    owner = ProductGitDeliveryCoreStore(cas.store)
    owner.persist(legacy.core, checkpoint=lambda: None)
    owner.persist_v2(case.core, checkpoint=lambda: None)
    reject(owner.persist_v2, legacy.core)
    reject(owner.persist, case.core)
    reject(owner.load_v2, legacy.core.fingerprint, code="git_delivery_core_read_failed")
    reject(owner.load, case.core.fingerprint, code="git_delivery_core_read_failed")
    reject(
        load_product_git_delivery_route_core,
        owner,
        case.plan.route,
        code="git_delivery_core_read_failed",
    )
    reject(materials.verify_product_git_delivery_core_materials_v2, cas, legacy.core)
    reject(materials.verify_product_git_delivery_core_materials, cas, case.core)


OBSERVATION_PATHS = [
    ("store_id",),
    ("key_id",),
    ("common_directory_path_sha256",),
    ("common_directory_identity",),
    ("git_directory_path_sha256",),
    ("git_directory_identity",),
    ("config_sha256",),
    ("implementation_digest",),
    ("index_file_observation", "identity"),
    ("index_file_observation", "sha256"),
    ("index_file_observation", "size"),
    ("baseline", "status_sha256"),
    ("baseline", "config_names_sha256"),
    ("baseline", "reader_binding"),
]


@pytest.mark.parametrize("path", OBSERVATION_PATHS)
def test_resealed_outer_core_and_plan_cannot_hide_changed_nested_observation(case, path):
    payload = core_payload(case.core)
    parent = at(payload["user_observation"], path[:-1])
    current = parent[path[-1]]
    parent[path[-1]] = (
        str(UUID(int=110))
        if path[-1] in {"store_id", "key_id"}
        else current + 1
        if type(current) is int
        else "0" * 64
    )
    # 原正文 SHA 成为合法新外层 FP，嵌套 U 指纹却必须仍失败。
    reject(decode_product_git_delivery_core_v2, canonical(payload))
    envelope = plan_payload(case.plan)
    envelope["core"] = rehash(payload)
    reject(decode_product_git_delivery_plan_v2, canonical(rehash(envelope)))


@pytest.mark.parametrize("field", ["store_id", "key_id"])
@pytest.mark.parametrize("side", ["core", "observation"])
def test_even_complete_reseal_requires_exact_matching_session_ids(case, field, side):
    payload = core_payload(case.core)
    target = payload if side == "core" else payload["user_observation"]
    target[field] = str(UUID(int=110))
    reseal_core_payload(payload)
    reject(
        decode_product_git_delivery_core_v2,
        canonical({k: v for k, v in payload.items() if k != "fingerprint"}),
    )
    envelope = plan_payload(case.plan)
    envelope["core"] = payload
    reject(decode_product_git_delivery_plan_v2, canonical(rehash(envelope)))


@pytest.mark.parametrize("path", OBSERVATION_PATHS)
def test_complete_observation_reseal_changes_full_core_and_envelope_fingerprints(case, path):
    """普通声明全部封签后可形成另一事实对象，但不保持旧资源或旧封套身份。"""
    payload = core_payload(case.core)
    parent = at(payload["user_observation"], path[:-1])
    current = parent[path[-1]]
    parent[path[-1]] = (
        str(UUID(int=110))
        if path[-1] in {"store_id", "key_id"}
        else current + 1
        if type(current) is int
        else "0" * 64
    )
    if path[-1] in {"store_id", "key_id"}:
        payload[path[-1]] = parent[path[-1]]
    other = ProductGitDeliveryCoreV2.model_validate_json(canonical(reseal_core_payload(payload)))
    envelope = plan_for(other, case.plan.review_artifact)
    assert other.user_observation.fingerprint != case.core.user_observation.fingerprint
    assert other.fingerprint != case.core.fingerprint
    assert envelope.fingerprint != case.plan.fingerprint
    assert (
        product_git_delivery_core_fingerprint(other)
        == hashlib.sha256(core_bytes(other)).hexdigest()
    )
    assert (
        product_git_delivery_plan_fingerprint(envelope)
        == hashlib.sha256(
            canonical({k: v for k, v in plan_payload(envelope).items() if k != "fingerprint"})
        ).hexdigest()
    )
    old_resource = product_git_delivery_resource(case.core)
    new_resource = product_git_delivery_resource(other)
    assert new_resource.attributes_sha256 == other.fingerprint != old_resource.attributes_sha256
    assert (new_resource.identifier_sha256 != old_resource.identifier_sha256) == (
        path[-1] in {"store_id", "key_id"}
    )


@pytest.mark.parametrize("nested", ["observation", "core", "plan"])
def test_only_the_current_fingerprint_is_excluded_and_nested_fingerprints_remain(case, nested):
    core = case.core
    if nested == "observation":
        forged = core.model_copy(
            update={
                "user_observation": core.user_observation.model_copy(
                    update={"fingerprint": "0" * 64}
                )
            }
        )
        assert product_git_delivery_core_fingerprint(forged) != core.fingerprint
        reject(snapshot_product_git_delivery_core_v2, forged)
    elif nested == "core":
        forged = core.model_copy(update={"fingerprint": "0" * 64})
        assert product_git_delivery_core_fingerprint(forged) == core.fingerprint
        envelope = case.plan.model_copy(update={"core": forged})
        assert product_git_delivery_plan_fingerprint(envelope) != case.plan.fingerprint
        reject(snapshot_product_git_delivery_plan_v2, envelope)
    else:
        forged = case.plan.model_copy(update={"fingerprint": "0" * 64})
        assert product_git_delivery_plan_fingerprint(forged) == case.plan.fingerprint
        reject(snapshot_product_git_delivery_plan_v2, forged)


MODEL_PATHS = [
    (),
    ("call",),
    ("user_observation",),
    ("user_observation", "baseline"),
    ("user_observation", "baseline", "source"),
    ("user_observation", "baseline", "source", "workspace"),
    ("user_observation", "index_file_observation"),
    ("anchor_intent",),
]


def object_at(value, path):
    for name in path:
        value = value[name] if type(name) is int else getattr(value, name)
    return value


def object_replace(value, path, replacement):
    if not path:
        return replacement
    if type(path[0]) is int:
        values = list(value)
        values[path[0]] = object_replace(value[path[0]], path[1:], replacement)
        return tuple(values)
    return value.model_copy(
        update={path[0]: object_replace(getattr(value, path[0]), path[1:], replacement)}
    )


@pytest.mark.parametrize("path", MODEL_PATHS)
@pytest.mark.parametrize("mode", ["subclass", "extra", "extra-state", "missing"])
def test_exact_deep_model_types_and_field_sets_are_required(case, path, mode):
    current = object_at(case.core, path)
    if mode == "subclass":
        kind = type("ForgedObservedSubclass", (type(current),), {})
        forged = kind.model_construct(**dict(vars(current)))
    else:
        forged = current.model_copy(deep=True)
        if mode == "extra":
            forged.__dict__["private-sensitive"] = "private-sensitive"
        elif mode == "extra-state":
            object.__setattr__(forged, "__pydantic_extra__", {"private-sensitive": True})
        else:
            del forged.__dict__[next(iter(type(current).model_fields))]
    core = object_replace(case.core, path, forged)
    reject(snapshot_product_git_delivery_core_v2, core)
    reject(encode_product_git_delivery_core_v2, core)
    reject(snapshot_product_git_delivery_plan_v2, case.plan.model_copy(update={"core": core}))


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("store_id", str(UUID(int=2))),
        ("diff_bytes", True),
        ("diff_bytes", "1"),
        ("user_observation", {}),
        ("object_scope", {}),
        ("call", {}),
    ],
)
def test_snapshot_does_not_coerce_wrong_python_containers(case, field, replacement):
    reject(snapshot_product_git_delivery_core_v2, case.core.model_copy(update={field: replacement}))


@pytest.mark.parametrize("field", ["objects", "external_history", "metrics", "roots", "limits"])
def test_full_scope_dataclasses_cannot_be_replaced_by_serialized_containers(case, field):
    scope = case.core.object_scope
    # 构造未经信任的输入，绕过局部构造检查，以验证正式深层入口也拒绝。
    forged = object.__new__(type(scope))
    for item in fields(scope):
        object.__setattr__(
            forged,
            item.name,
            (
                json_facts(getattr(scope, field))
                if item.name == field
                else getattr(scope, item.name)
            ),
        )
    reject(
        snapshot_product_git_delivery_core_v2, case.core.model_copy(update={"object_scope": forged})
    )


def test_scope_subclass_and_tuple_to_list_are_rejected(case):
    scope = case.core.object_scope
    kind = type("ForgedScopeSubclass", (GitInventoryScope,), {})
    subclass = object.__new__(kind)
    for field in fields(scope):
        object.__setattr__(subclass, field.name, getattr(scope, field.name))
    reject(
        snapshot_product_git_delivery_core_v2,
        case.core.model_copy(update={"object_scope": subclass}),
    )
    source = case.core.baseline.source
    altered = source.model_copy(update={"mutations": list(source.mutations)})
    observation = case.core.user_observation.model_copy(
        update={"baseline": case.core.baseline.model_copy(update={"source": altered})}
    )
    reject(
        snapshot_product_git_delivery_core_v2,
        case.core.model_copy(update={"user_observation": observation}),
    )


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("user_observation",),
        ("user_observation", "index_file_observation"),
        ("user_observation", "baseline"),
        ("user_observation", "baseline", "source"),
        ("object_scope",),
        ("object_scope", "objects", 0),
    ],
)
def test_wire_extra_fields_at_every_layer_are_not_dropped(case, path):
    payload = core_payload(case.core)
    at(payload, path)["private-sensitive"] = "private-sensitive"
    reject(decode_product_git_delivery_core_v2, canonical(payload))


@pytest.mark.parametrize(
    "path,field",
    [
        ((), "spec_version"),
        ((), "user_observation"),
        ((), "anchor_intent"),
        ((), "commit_spec"),
        (("user_observation",), "spec_version"),
        (("user_observation",), "config_sha256"),
        (("user_observation",), "fingerprint"),
        (("user_observation", "baseline"), "spec_version"),
        (("user_observation", "baseline", "source"), "spec_version"),
        (("object_scope",), "action_kind"),
    ],
)
def test_wire_missing_fields_never_trigger_default_completion(case, path, field):
    payload = core_payload(case.core)
    del at(payload, path)[field]
    reject(decode_product_git_delivery_core_v2, canonical(payload))


@pytest.mark.parametrize(
    "name",
    [
        "baseline",
        "common_directory_path_sha256",
        "common_directory_identity",
        "index_file_observation",
    ],
)
def test_legacy_aliases_cannot_supply_or_shadow_observation(case, name):
    payload = core_payload(case.core)
    payload[name] = json_facts(getattr(case.core, name))
    reject(decode_product_git_delivery_core_v2, canonical(payload))
    payload.pop("user_observation")
    reject(decode_product_git_delivery_core_v2, canonical(payload))


@pytest.mark.parametrize("level", ["root", "observation", "index", "source"])
@pytest.mark.parametrize("kind", ["core", "plan"])
def test_duplicate_json_keys_even_identical_are_rejected(case, level, kind):
    payload = core_payload(case.core) if kind == "core" else plan_payload(case.plan)
    body = canonical(payload)
    if level == "root":
        body = b'{"spec_version":' + canonical(payload["spec_version"]) + b"," + body[1:]
    else:
        core = payload if kind == "core" else payload["core"]
        path = {
            "observation": ("user_observation",),
            "index": ("user_observation", "index_file_observation"),
            "source": ("user_observation", "baseline", "source"),
        }[level]
        target = at(core, path)
        key = sorted(target)[0]
        original = canonical(target)
        duplicate = b"{" + canonical(key) + b":" + canonical(target[key]) + b"," + original[1:]
        assert original in body
        body = body.replace(original, duplicate, 1)
    reject(
        decode_product_git_delivery_core_v2
        if kind == "core"
        else decode_product_git_delivery_plan_v2,
        body,
    )


@pytest.mark.parametrize(
    "mode",
    [
        "spaces",
        "unsorted",
        "ascii",
        "lf",
        "bom",
        "root-list",
        "null",
        "nan",
        "invalid-utf8",
        "fingerprint",
    ],
)
@pytest.mark.parametrize("kind", ["core", "plan"])
def test_noncanonical_and_non_json_wire_is_rejected(case, mode, kind):
    payload = core_payload(case.core) if kind == "core" else plan_payload(case.plan)
    body = canonical(payload)
    bodies = {
        "spaces": json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
        "unsorted": json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        "ascii": json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode(),
        "lf": body + b"\n",
        "bom": b"\xef\xbb\xbf" + body,
        "root-list": b"[" + body + b"]",
        "null": b"null",
        "nan": b'{"value":NaN}',
        "invalid-utf8": b"\xff" + body,
        "fingerprint": canonical({**payload, "fingerprint": "0" * 64}),
    }
    # Checkpoint 的纯声明正文无中文；用 Unicode 转义规范键测试同义字节拒绝。
    if mode == "ascii":
        bodies[mode] = body.replace(b'"spec_version"', b'"\\u0073pec_version"', 1)
    reject(
        decode_product_git_delivery_core_v2
        if kind == "core"
        else decode_product_git_delivery_plan_v2,
        bodies[mode],
    )


@pytest.mark.parametrize(
    "factory",
    [str, bytearray, memoryview, list, lambda value: type("BytesSubclass", (bytes,), {})(value)],
)
@pytest.mark.parametrize("kind", ["core", "plan"])
def test_wire_requires_exact_bytes_not_convertible_containers(case, factory, kind):
    body = core_bytes(case.core) if kind == "core" else canonical(plan_payload(case.plan))
    reject(
        decode_product_git_delivery_core_v2
        if kind == "core"
        else decode_product_git_delivery_plan_v2,
        factory(body),
    )


@pytest.mark.parametrize("mode", ["upper", "odd", "invalid", "name_hex"])
def test_full_scope_tree_names_use_only_canonical_lowercase_hex(case, mode):
    payload = core_payload(case.core)
    entry = next(
        entry for node in payload["object_scope"]["objects"] for entry in node["tree_entries"]
    )
    if mode == "upper":
        entry["name"] = entry["name"].upper()
        if entry["name"] == entry["name"].lower():
            entry["name"] = "6B656570"
    elif mode == "odd":
        entry["name"] += "0"
    elif mode == "invalid":
        entry["name"] = "zz"
    else:
        entry["name_hex"] = entry.pop("name")
    reject(decode_product_git_delivery_core_v2, canonical(payload))


@pytest.mark.parametrize("kind", ["core", "plan"])
def test_bounded_wire_rejects_empty_or_over_512k_without_partial_success(case, kind):
    assert MAX_PRODUCT_GIT_PLAN_BYTES == 512 * 1024
    decoder = (
        decode_product_git_delivery_core_v2
        if kind == "core"
        else decode_product_git_delivery_plan_v2
    )
    reject(decoder, b"")
    reject(decoder, b" " * (512 * 1024 + 1))


@pytest.mark.parametrize("kind", ["core", "plan"])
def test_complete_canonical_oversized_commit_is_rejected_not_truncated(cas, tmp_path, kind):
    # 字符数仍在原合法上限内，容量按完整 UTF-8 字节而不是字符数计算。
    case, _legacy = make_observed_case(
        cas, tmp_path, action="commit", commit_message="🙂" * 65535 + "\n"
    )
    owner = ProductGitDeliveryCoreStore(cas.store)
    before = observe_state(case)
    if kind == "core":
        expected = core_bytes(case.core)
        reject(encode_product_git_delivery_core_v2, case.core)
        reject(decode_product_git_delivery_core_v2, expected)
        reject(owner.persist_v2, case.core)
    else:
        expected = canonical(plan_payload(case.plan))
        reject(encode_product_git_delivery_plan_v2, case.plan)
        reject(decode_product_git_delivery_plan_v2, expected)
    assert len(expected) > 512 * 1024
    assert observe_state(case) == before


@pytest.mark.parametrize(
    "role",
    [
        "base_commit",
        "base_tree",
        "target_tree",
        "tree_member",
        "delivery_commit",
        "parent_manifest",
        "parent_chunk",
    ],
)
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_any_missing_or_corrupt_complete_material_prevents_success(
    cas, tmp_path, role, damage, fmt
):
    case, _legacy = make_observed_case(cas, tmp_path, action="commit", fmt=fmt)
    if role.startswith("parent_"):
        digest = case.core.baseline.source.workspace.parent_closure.sha256
        if role == "parent_chunk":
            digest = json.loads(cas.store.blob(digest))["chunks"][0]["sha256"]
    else:
        node = next(node for node in case.core.object_scope.objects if role in node.roles)
        digest = node.material.cas_digest
    blob = cas.store._blobs / digest
    original = blob.read_bytes()
    moved = blob.with_name(blob.name + ".missing")
    if damage == "missing":
        blob.rename(moved)
    else:
        blob.write_bytes(b"private-sensitive-corruption")
    try:
        before = observe_state(case)
        for operation in (
            materials.verify_product_git_delivery_core_materials_v2,
            materials.read_product_git_delivery_core_materials_v2,
        ):
            with pytest.raises(KernelError) as caught:
                readonly(case, operation, cas, case.core)
            assert caught.value.__cause__ is None
            assert "private-sensitive" not in str(caught.value)
        assert observe_state(case) == before
    finally:
        if damage == "missing":
            moved.rename(blob)
        else:
            blob.write_bytes(original)


@pytest.mark.parametrize("field,value", [("diff_sha256", "0" * 64), ("diff_bytes", 1)])
def test_resealed_wrong_full_diff_is_not_a_material_success(case, field, value):
    other = rebuild(case.core, **{field: value})
    reject(materials.verify_product_git_delivery_core_materials_v2, case.cas, other)
    reject(materials.read_product_git_delivery_core_materials_v2, case.cas, other)


@pytest.mark.parametrize("mode", ["before-content", "after-mode", "baseline-member"])
def test_all_nested_reseals_cannot_hide_source_or_baseline_material_divergence(case, mode):
    payload = core_payload(case.core)
    baseline = payload["user_observation"]["baseline"]
    source = baseline["source"]
    keep = next(
        material
        for material in case.bodies
        if material.object_type == "blob" and material.body == b"unchanged\n"
    )
    if mode == "before-content":
        source["mutations"][0]["before"].update(sha256=keep.body_sha256, size=keep.body_bytes)
    elif mode == "after-mode":
        source["mutations"][0]["after"]["mode"] = 0o644
    else:
        baseline["members"][0]["oid"] = keep.object_id
    other = ProductGitDeliveryCoreV2.model_validate_json(canonical(reseal_core_payload(payload)))
    for operation in (
        materials.verify_product_git_delivery_core_materials_v2,
        materials.read_product_git_delivery_core_materials_v2,
    ):
        with pytest.raises(KernelError):
            readonly(case, operation, case.cas, other)


@pytest.mark.parametrize(
    "mode",
    ["drop-object", "duplicate-object", "drop-parent", "metrics", "limit", "extra-object-field"],
)
def test_outer_reseal_never_trims_full_scope_or_parent_edges(case, mode):
    payload = core_payload(case.core)
    scope = payload["object_scope"]
    if mode == "drop-object":
        scope["objects"].pop()
    elif mode == "duplicate-object":
        scope["objects"].append(scope["objects"][0])
    elif mode == "drop-parent":
        scope["external_history"]["parents"].pop()
    elif mode == "metrics":
        scope["metrics"]["objects"] = 0
    elif mode == "limit":
        scope["limits"]["max_objects"] = 0
    else:
        scope["objects"][0]["approved"] = True
    reject(decode_product_git_delivery_core_v2, canonical(payload))


@pytest.mark.parametrize(
    "value", [None, 1, True, "A" * 64, "0" * 63, "g" * 64, "../private-sensitive", b"0" * 64]
)
def test_cas_load_rejects_non_exact_content_addresses(case, value):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    before = observe_state(case)
    reject(owner.load_v2, value, code="git_delivery_core_read_failed")
    assert observe_state(case) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_core_cas_damage_fails_direct_and_route_recovery(case, damage):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    saved = owner.persist_v2(case.core, checkpoint=lambda: None)
    blob = case.cas.store._blobs / saved.fingerprint
    if damage == "missing":
        blob.rename(blob.with_name(blob.name + ".missing"))
    else:
        blob.write_bytes(b"private-sensitive-corrupt-core")
    before = observe_state(case)
    reject(owner.load_v2, saved.fingerprint, code="git_delivery_core_read_failed")
    reject(
        load_product_git_delivery_route_core_v2,
        owner,
        case.plan.route,
        code="git_delivery_core_read_failed",
    )
    assert observe_state(case) == before


@pytest.mark.parametrize("kind", ["missing-observation", "noncanonical", "duplicate", "legacy"])
def test_even_correct_cas_sha_cannot_legitimize_invalid_core_wire(cas, tmp_path, kind):
    case, legacy = make_observed_case(cas, tmp_path)
    payload = core_payload(case.core)
    if kind == "missing-observation":
        del payload["user_observation"]
        body = canonical(payload)
    elif kind == "noncanonical":
        body = core_bytes(case.core) + b"\n"
    elif kind == "duplicate":
        body = b'{"diff_bytes":' + canonical(payload["diff_bytes"]) + b"," + canonical(payload)[1:]
    else:
        body = canonical({k: v for k, v in json_facts(legacy.core).items() if k != "fingerprint"})
    digest = hashlib.sha256(body).hexdigest()
    cas.store.put_blob(digest, body)
    owner = ProductGitDeliveryCoreStore(cas.store)
    before = observe_state(case)
    reject(owner.load_v2, digest, code="git_delivery_core_read_failed")
    assert cas.store.blob(digest) == body and observe_state(case) == before


@pytest.mark.parametrize("port", ["put_blob", "blob"])
@pytest.mark.parametrize("error_kind", ["io", "spoof-control"])
def test_untrusted_cas_errors_are_sanitized_not_treated_as_real_control(
    case, monkeypatch, port, error_kind
):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    owner.persist_v2(case.core, checkpoint=lambda: None)
    error = (
        OSError("private-sensitive-io")
        if error_kind == "io"
        else KernelError("cancelled", "private-sensitive-spoof")
    )

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, port, fail)
    caught = reject(owner.persist_v2, case.core, code="git_delivery_core_write_failed")
    assert caught is not error
    if port == "blob":
        caught = reject(owner.load_v2, case.core.fingerprint, code="git_delivery_core_read_failed")
        assert caught is not error


@pytest.mark.parametrize(
    "factory",
    [str, bytearray, memoryview, lambda value: type("BytesSubclass", (bytes,), {})(value)],
)
def test_cas_readback_requires_exact_bytes_and_never_returns_partial_core(
    case, monkeypatch, factory
):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    body = core_bytes(case.core)
    monkeypatch.setattr(
        SQLiteWorkspaceTransactionStore, "blob", lambda *args, **kwargs: factory(body)
    )
    reject(owner.persist_v2, case.core, code="git_delivery_core_write_failed")
    reject(owner.load_v2, case.core.fingerprint, code="git_delivery_core_read_failed")


@pytest.mark.parametrize(
    "mode", ["empty", "extra", "kind", "access", "identifier", "attributes", "unrelated-address"]
)
def test_route_requires_unique_full_matching_external_write_resource(case, mode):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    owner.persist_v2(case.core, checkpoint=lambda: None)
    resource = case.plan.route.resources[0]
    if mode == "empty":
        resources = ()
    elif mode == "extra":
        other = resource.model_copy(update={"identifier_sha256": "0" * 64})
        resources = tuple(
            sorted(
                (resource, other),
                key=lambda r: (r.kind, r.access, r.identifier_sha256, r.attributes_sha256),
            )
        )
    else:
        updates = {
            "kind": {"kind": "workspace"},
            "access": {"access": "read"},
            "identifier": {"identifier_sha256": "0" * 64},
            "attributes": {"attributes_sha256": "0" * 64},
            "unrelated-address": {
                "attributes_sha256": case.core.baseline.source.workspace.parent_closure.sha256
            },
        }[mode]
        resources = (resource.model_copy(update=updates),)
    route = resource_route(case.plan.route, resources)
    code = (
        "git_delivery_core_read_failed"
        if mode in {"attributes", "unrelated-address"}
        else "git_delivery_plan_invalid"
    )
    reject(load_product_git_delivery_route_core_v2, owner, route, code=code)


@pytest.mark.parametrize(
    "field",
    [
        "store_id",
        "key_id",
        "thread_id",
        "turn_id",
        "call_id",
        "tool_version",
        "tool_fingerprint",
        "patch-arguments",
    ],
)
def test_resealed_route_and_attributes_cannot_skip_any_core_identity_or_call(case, field):
    core = case.core
    payload = core_payload(core)
    if field in {"store_id", "key_id"}:
        payload[field] = payload["user_observation"][field] = str(UUID(int=110))
    elif field == "thread_id":
        payload[field] = payload["user_observation"]["baseline"]["source"][field] = str(
            UUID(int=110)
        )
    elif field == "turn_id":
        payload[field] = str(UUID(int=110))
    elif field == "call_id":
        payload["call"][field] = str(UUID(int=110))
    elif field == "patch-arguments":
        payload["call"]["arguments"]["patches"] = [str(UUID(int=110))]
        payload["user_observation"]["baseline"]["source"]["patches"][0]["transaction_id"] = str(
            UUID(int=110)
        )
    else:
        payload["call"][field] = "2" if field == "tool_version" else "0" * 64
    changed = rebind_identity(
        ProductGitDeliveryCoreV2.model_validate_json(canonical(reseal_core_payload(payload)))
    )
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    owner.persist_v2(changed, checkpoint=lambda: None)
    original = case.plan.route.resources[0]
    # 保留旧 Route 的全部身份，仅把属性指向全封签新 Core；不能只核对 FP。
    route = resource_route(
        case.plan.route, (original.model_copy(update={"attributes_sha256": changed.fingerprint}),)
    )
    reject(load_product_git_delivery_route_core_v2, owner, route)


@pytest.mark.parametrize("mode", ["workspace", "policy", "recovery", "claimed-delivery"])
def test_route_complete_execution_and_derived_identity_are_not_replaceable(case, mode):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    core = case.core
    if mode == "claimed-delivery":
        core = rebuild(core, delivery_id=UUID(int=110))
        route = route_for(core)
    elif mode == "policy":
        route = route_for(core, decision=PolicyDecisionKind.ALLOW)
    elif mode == "workspace":
        execution = case.plan.route.execution
        other, _legacy = make_observed_case(case.cas, case.workspace_root.parent / "other")
        workspace = other.core.baseline.source.workspace
        route = seal(
            ActionRoutePlanV2,
            {
                **model_fields(case.plan.route),
                "execution": seal(
                    type(execution), {**model_fields(execution), "workspace": workspace}
                ),
            },
        )
    else:
        binding = seal(
            type(case.plan.route.binding),
            {
                **model_fields(case.plan.route.binding, "binding_digest"),
                "recovery_mode": "durable_ledger",
            },
            "binding_digest",
        )
        route = seal(
            ActionRoutePlanV2,
            {**model_fields(case.plan.route), "binding": binding, "external_action_id": None},
        )
    owner.persist_v2(core, checkpoint=lambda: None)
    reject(load_product_git_delivery_route_core_v2, owner, route)


@pytest.mark.parametrize(
    "mode", ["subclass", "extra", "wrong-resources", "owner-subclass", "owner-proxy"]
)
def test_route_and_owner_actual_types_are_strict(case, mode):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    owner.persist_v2(case.core, checkpoint=lambda: None)
    route = case.plan.route
    if mode == "subclass":
        route = type("ForgedRouteSubclass", (ActionRoutePlanV2,), {}).model_construct(**vars(route))
    elif mode == "extra":
        route = route.model_copy(deep=True)
        route.__dict__["private-sensitive"] = True
    elif mode == "wrong-resources":
        route = route.model_copy(update={"resources": list(route.resources)})
    elif mode == "owner-subclass":
        owner = type("ForgedCoreStoreSubclass", (ProductGitDeliveryCoreStore,), {})(case.cas.store)
    else:
        owner = object()
    reject(load_product_git_delivery_route_core_v2, owner, route)


@pytest.mark.parametrize(
    "phase",
    [
        "snapshot-core",
        "snapshot-plan",
        "encode-core",
        "encode-plan",
        "decode-core",
        "decode-plan",
        "persist",
        "load",
        "route",
        "verify",
        "read",
    ],
)
@pytest.mark.parametrize("trip", [1, 100])
@pytest.mark.parametrize(
    "error_kind", ["value", "type", "recursion", "cancel", "timeout", "kernel", "base-cancel"]
)
def test_all_v2_boundaries_preserve_exact_checkpoint_exception_identity(
    case, phase, trip, error_kind
):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    owner.persist_v2(case.core, checkpoint=lambda: None)
    operations = {
        "snapshot-core": (snapshot_product_git_delivery_core_v2, (case.core,)),
        "snapshot-plan": (snapshot_product_git_delivery_plan_v2, (case.plan,)),
        "encode-core": (encode_product_git_delivery_core_v2, (case.core,)),
        "encode-plan": (encode_product_git_delivery_plan_v2, (case.plan,)),
        "decode-core": (decode_product_git_delivery_core_v2, (core_bytes(case.core),)),
        "decode-plan": (decode_product_git_delivery_plan_v2, (canonical(plan_payload(case.plan)),)),
        "persist": (owner.persist_v2, (case.core,)),
        "load": (owner.load_v2, (case.core.fingerprint,)),
        "route": (load_product_git_delivery_route_core_v2, (owner, case.plan.route)),
        "verify": (materials.verify_product_git_delivery_core_materials_v2, (case.cas, case.core)),
        "read": (materials.read_product_git_delivery_core_materials_v2, (case.cas, case.core)),
    }
    error = {
        "value": ValueError("private-sensitive-control"),
        "type": TypeError("private-sensitive-control"),
        "recursion": RecursionError("private-sensitive-control"),
        "cancel": TurnCancelled(),
        "timeout": TimeoutError("private-sensitive-control"),
        "kernel": KernelError("git_delivery_plan_invalid", "private-sensitive-control"),
        "base-cancel": asyncio.CancelledError("private-sensitive-control"),
    }[error_kind]
    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == trip:
            raise error

    operation, args = operations[phase]
    before = observe_state(case)
    with pytest.raises(type(error)) as caught:
        operation(*args, checkpoint=checkpoint)
    assert caught.value is error and count == trip
    assert observe_state(case) == before


@pytest.mark.parametrize("port", ["put_blob", "blob"])
def test_actual_checkpoint_inside_cas_io_preserves_original_control(case, monkeypatch, port):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    error = ValueError("private-sensitive-control")
    active = False

    def checkpoint():
        if active:
            raise error

    def fail(*args, checkpoint, **kwargs):
        nonlocal active
        active = True
        checkpoint()
        pytest.fail("原检查点应立即传播")

    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, port, fail)
    with pytest.raises(ValueError) as caught:
        owner.persist_v2(case.core, checkpoint=checkpoint)
    assert caught.value is error


@pytest.mark.parametrize(
    "field", ["fingerprint", "config_sha256", "implementation_digest", "git_directory_identity"]
)
def test_direct_core_constructor_rechecks_existing_observation_bad_fingerprint(case, field):
    """回归 Pydantic 实例快速路径：既有坏对象即使外层重封签也不得构造成功。"""
    observation = case.core.user_observation.model_copy(update={field: "0" * 64})
    candidate = case.core.model_copy(update={"user_observation": observation})
    digest = hashlib.sha256(core_bytes(candidate)).hexdigest()
    values = {**model_fields(candidate), "fingerprint": digest}
    with pytest.raises(ValidationError):
        ProductGitDeliveryCoreV2(**values)
    with pytest.raises(ValidationError):
        ProductGitDeliveryCoreV2.model_validate(values, strict=True)
    reject(
        snapshot_product_git_delivery_core_v2, candidate.model_copy(update={"fingerprint": digest})
    )


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("route",),
        ("route", "invocation"),
        ("route", "binding"),
        ("route", "resources", 0),
        ("route", "execution"),
        ("route", "execution", "policy"),
        ("route", "execution", "sandbox"),
        ("review_artifact",),
    ],
)
@pytest.mark.parametrize("mode", ["subclass", "extra", "missing"])
def test_full_plan_envelope_and_artifact_exact_models_are_required(case, path, mode):
    current = object_at(case.plan, path)
    if mode == "subclass":
        forged = type("ForgedPlanModelSubclass", (type(current),), {}).model_construct(
            **vars(current)
        )
    else:
        forged = current.model_copy(deep=True)
        if mode == "extra":
            forged.__dict__["private-sensitive"] = True
        else:
            del forged.__dict__[next(iter(type(current).model_fields))]
    plan = object_replace(case.plan, path, forged)
    reject(snapshot_product_git_delivery_plan_v2, plan)
    reject(encode_product_git_delivery_plan_v2, plan)


@pytest.mark.parametrize(
    "field", ["spec_version", "core", "route", "review_artifact", "fingerprint"]
)
def test_plan_v2_does_not_complete_missing_wire_fields(case, field):
    payload = plan_payload(case.plan)
    del payload[field]
    reject(decode_product_git_delivery_plan_v2, canonical(payload))


def test_absent_physical_index_roundtrips_without_logical_index_alias(case):
    observation = case.core.user_observation
    index = type(observation.index_file_observation)(
        presence="absent", identity=None, sha256=None, size=0
    )
    altered = seal(
        type(observation), {**model_fields(observation), "index_file_observation": index}
    )
    core = rebuild(case.core, user_observation=altered)
    body = core_bytes(core)
    result = decode_product_git_delivery_core_v2(body, checkpoint=lambda: None)
    assert result.index_file_observation == index
    assert result.baseline.index_observation_bytes == 42
    assert result.fingerprint != case.core.fingerprint


@pytest.mark.parametrize("kind", ["core", "plan"])
def test_exact_512k_canonical_body_is_accepted_but_one_more_byte_is_rejected(cas, tmp_path, kind):
    """真实合法 Commit 在原边界精确通过；不提高预算，也不返回截断正文。"""

    def expected(case):
        return core_bytes(case.core) if kind == "core" else canonical(plan_payload(case.plan))

    ascii_message = "a" * 65535 + "\n"
    small, _legacy = make_observed_case(
        cas,
        tmp_path / "size-0000",
        action="commit",
        commit_message=ascii_message,
        provider_call_id="p",
    )
    repeated = 2 if kind == "core" else 4
    assert expected(small).count(canonical(ascii_message)) == repeated
    # 留少量余量给正文长度指标的十进制进位，再用唯一 provider 字段补齐字节。
    extra = (512 * 1024 - len(expected(small)) - 64) // repeated
    emojis, accents = divmod(extra, 3)
    assert 0 <= emojis + accents <= 65535
    message = "🙂" * emojis + "é" * accents + "a" * (65535 - emojis - accents) + "\n"
    near, _legacy = make_observed_case(
        cas,
        tmp_path / "size-0001",
        action="commit",
        commit_message=message,
        provider_call_id="p",
    )
    padding = 512 * 1024 - len(expected(near))
    assert 0 <= padding <= 254
    exact, _legacy = make_observed_case(
        cas,
        tmp_path / "size-0002",
        action="commit",
        commit_message=message,
        provider_call_id="p" * (padding + 1),
    )
    body = expected(exact)
    assert len(body) == 512 * 1024
    encoder = (
        encode_product_git_delivery_core_v2
        if kind == "core"
        else encode_product_git_delivery_plan_v2
    )
    decoder = (
        decode_product_git_delivery_core_v2
        if kind == "core"
        else decode_product_git_delivery_plan_v2
    )
    value = exact.core if kind == "core" else exact.plan
    assert encoder(value, checkpoint=lambda: None) == body
    assert decoder(body, checkpoint=lambda: None) == value
    over, _legacy = make_observed_case(
        cas,
        tmp_path / "size-0003",
        action="commit",
        commit_message=message,
        provider_call_id="p" * (padding + 2),
    )
    assert len(expected(over)) == 512 * 1024 + 1
    reject(encoder, over.core if kind == "core" else over.plan)
    reject(decoder, expected(over))
    if kind == "core":
        owner = ProductGitDeliveryCoreStore(cas.store)
        assert owner.persist_v2(exact.core, checkpoint=lambda: None) == exact.core
        assert owner.load_v2(exact.core.fingerprint, checkpoint=lambda: None) == exact.core
        reject(owner.persist_v2, over.core)


def test_snapshot_and_cas_models_detach_all_mutable_argument_aliases(case):
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    saved = owner.persist_v2(case.core, checkpoint=lambda: None)
    body = case.cas.store.blob(saved.fingerprint)
    snapshot = snapshot_product_git_delivery_plan_v2(case.plan, checkpoint=lambda: None)
    assert snapshot.core.call.arguments is not case.core.call.arguments
    assert snapshot.core.call.arguments is not snapshot.route.invocation.arguments
    assert snapshot.route.invocation.arguments is not snapshot.route.execution.intent.arguments
    assert (
        snapshot.core.user_observation.baseline.source.workspace
        is not snapshot.route.execution.workspace
    )
    saved.call.arguments["patches"].append(str(UUID(int=110)))
    snapshot.route.invocation.arguments["patches"].append(str(UUID(int=111)))
    assert case.core.call.arguments["patches"] == [str(UUID(int=10))]
    assert owner.load_v2(case.core.fingerprint, checkpoint=lambda: None) == case.core
    assert case.cas.store.blob(case.core.fingerprint) == body


@pytest.mark.parametrize("mode", ["subclass", "proxy"])
def test_core_store_requires_original_store_actual_type_without_io(case, mode):
    supplied = object()
    if mode == "subclass":
        kind = type("ForgedOriginalStoreSubclass", (SQLiteWorkspaceTransactionStore,), {})
        supplied = object.__new__(kind)
    before = observe_state(case)
    with pytest.raises(KernelError) as caught:
        ProductGitDeliveryCoreStore(supplied)
    assert caught.value.code == "git_delivery_core_store_invalid"
    assert observe_state(case) == before


def api_case(cas, path, version):
    """两代普通声明使用真实 CAS；旧代际只重绑原派生身份，不升级为新代际。"""
    observed, legacy = make_observed_case(cas, path)
    if version == 2:
        return observed
    preliminary = route_for(legacy.core)
    delivery_id = external_action_identity(preliminary.invocation, preliminary.binding)
    core = seal(ProductGitDeliveryCore, {**model_fields(legacy.core), "delivery_id": delivery_id})
    plan = seal(
        ProductGitDeliveryPlan,
        {
            "core": core,
            "route": route_for(core),
            "review_artifact": legacy.plan.review_artifact,
        },
    )
    return replace(legacy, core=core, plan=plan)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["core", "plan"])
def test_public_snapshot_injection_is_observed_by_encode_and_decode(
    cas, tmp_path, monkeypatch, version, kind
):
    """公共入口保留可观测注入边界；底层共享算法不能静默绕过原 API。"""
    case = api_case(cas, tmp_path, version)
    suffix = "" if version == 1 else "_v2"
    encoder_module = wire_module if version == 1 else observed_wire_module
    value = case.core if kind == "core" else case.plan
    name = f"snapshot_product_git_delivery_{kind}{suffix}"
    hooks = Counter()
    targets = (encoder_module,) if version == 1 else (encoder_module, wire_module)
    for target in targets:
        original = getattr(target, name)

        def snapshot(actual, *, checkpoint, original=original, target=target):
            hooks[target.__name__] += 1
            assert type(actual) is type(value)
            return original(actual, checkpoint=checkpoint)

        monkeypatch.setattr(target, name, snapshot)
    facts = json_facts(value)
    if kind == "core":
        facts.pop("fingerprint")
    expected = canonical(facts)
    encoder = getattr(encoder_module, f"encode_product_git_delivery_{kind}{suffix}")
    decoder = getattr(encoder_module, f"decode_product_git_delivery_{kind}{suffix}")
    assert encoder(value, checkpoint=lambda: None) == expected
    assert decoder(expected, checkpoint=lambda: None) == value
    assert sum(hooks.values()) == 2
    assert hooks[encoder_module.__name__] == (2 if version == 1 else 1)


@pytest.mark.parametrize("version", [1, 2])
def test_core_store_preserves_public_snapshot_codec_and_material_snapshot_hooks(
    cas, tmp_path, monkeypatch, version
):
    case = api_case(cas, tmp_path, version)
    suffix = "" if version == 1 else "_v2"
    owner = ProductGitDeliveryCoreStore(cas.store)
    calls = Counter()
    for prefix in ("snapshot", "encode", "decode"):
        name = f"{prefix}_product_git_delivery_core{suffix}"
        original = getattr(store_module, name)

        def hook(actual, *, checkpoint, prefix=prefix, original=original):
            calls[prefix] += 1
            return original(actual, checkpoint=checkpoint)

        monkeypatch.setattr(store_module, name, hook)
    saved = getattr(owner, "persist" + suffix)(case.core, checkpoint=lambda: None)
    assert calls == {"snapshot": 1, "encode": 1, "decode": 1}
    assert getattr(owner, "load" + suffix)(saved.fingerprint, checkpoint=lambda: None) == saved
    assert calls == {"snapshot": 1, "encode": 1, "decode": 2}
    expected = json_facts(case.core)
    expected.pop("fingerprint")
    assert cas.store.blob(saved.fingerprint) == canonical(expected)
    name = "snapshot_product_git_delivery_core" + suffix
    original_snapshot = getattr(materials, name)

    def snapshot(actual, *, checkpoint):
        calls["material-snapshot"] += 1
        return original_snapshot(actual, checkpoint=checkpoint)

    monkeypatch.setattr(materials, name, snapshot)
    verified = getattr(materials, "verify_product_git_delivery_core_materials" + suffix)(
        cas, case.core, checkpoint=lambda: None
    )
    assert verified == case.core and calls["material-snapshot"] == 1


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("changed", [False, True])
def test_route_consumes_public_load_result_and_rechecks_it(
    cas, tmp_path, monkeypatch, version, changed
):
    case = api_case(cas, tmp_path, version)
    suffix = "" if version == 1 else "_v2"
    owner = ProductGitDeliveryCoreStore(cas.store)
    getattr(owner, "persist" + suffix)(case.core, checkpoint=lambda: None)
    original_load = getattr(ProductGitDeliveryCoreStore, "load" + suffix)
    calls = []

    def load(actual, digest, *, checkpoint):
        calls.append(digest)
        restored = original_load(actual, digest, checkpoint=checkpoint)
        # 公共恢复结果重新深层核验；注入损坏声明不应被丢弃或静默重新私有读取。
        return restored.model_copy(update={"diff_sha256": "0" * 64}) if changed else restored

    monkeypatch.setattr(ProductGitDeliveryCoreStore, "load" + suffix, load)
    operation = (
        load_product_git_delivery_route_core
        if version == 1
        else load_product_git_delivery_route_core_v2
    )
    if changed:
        reject(operation, owner, case.plan.route)
    else:
        assert readonly(case, operation, owner, case.plan.route) == case.core
    assert calls == [case.core.fingerprint]


def test_observed_wire_reexports_same_public_v2_snapshot_functions(case):
    assert (
        observed_wire_module.snapshot_product_git_delivery_core_v2
        is snapshot_module.snapshot_product_git_delivery_core_v2
    )
    assert (
        observed_wire_module.snapshot_product_git_delivery_plan_v2
        is snapshot_module.snapshot_product_git_delivery_plan_v2
    )
    assert (
        snapshot_module.snapshot_product_git_delivery_core_v2(case.core, checkpoint=lambda: None)
        == case.core
    )
    assert (
        snapshot_module.snapshot_product_git_delivery_plan_v2(case.plan, checkpoint=lambda: None)
        == case.plan
    )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "operation,port", [("persist", "put_blob"), ("persist", "blob"), ("load", "blob")]
)
@pytest.mark.parametrize("transport", ["direct", "wrapped"])
def test_caller_upstream_marker_inside_io_retains_same_outer_exception_object(
    cas, tmp_path, monkeypatch, version, operation, port, transport
):
    """调用方直接抛同类 marker 时保留其身份；原 CAS 再包一层也不能误解包。"""
    case = api_case(cas, tmp_path, version)
    suffix = "" if version == 1 else "_v2"
    owner = ProductGitDeliveryCoreStore(cas.store)
    getattr(owner, "persist" + suffix)(case.core, checkpoint=lambda: None)
    marker = UpstreamCheckpointError(ValueError("private-sensitive-original"))
    active = False

    def checkpoint():
        if active:
            raise marker

    def fail(*args, checkpoint, **kwargs):
        nonlocal active
        active = True
        if transport == "wrapped":
            try:
                checkpoint()
            except BaseException as error:
                raise UpstreamCheckpointError(error) from None
        else:
            checkpoint()
        pytest.fail("控制 marker 必须立即传播")

    monkeypatch.setattr(SQLiteWorkspaceTransactionStore, port, fail)
    argument = case.core if operation == "persist" else case.core.fingerprint
    with pytest.raises(UpstreamCheckpointError) as caught:
        getattr(owner, operation + suffix)(argument, checkpoint=checkpoint)
    assert caught.value is marker
    assert caught.value.error is marker.error
