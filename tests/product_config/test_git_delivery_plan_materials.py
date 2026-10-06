"""原 SQLite CAS 的 Git 计划全材料复核；成功也不构成批准或 Git 写效果。"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_inventory_contracts import GitInventoryObject
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_delivery_plan_contracts import ProductGitDeliveryCore
from harnessix.product_config.git_delivery_plan_materials import (
    verify_product_git_delivery_core_materials,
)
from tests.delivery.test_git_inventory_contracts import _tree
from tests.product_config.git_delivery_plan_support import (
    ZERO,
    _scope,
    canonical_bytes,
    make_case,
    observe_state,
    rehash,
    sealed,
)


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


def verify(case, core=None, *, checkpoint=lambda: None):
    """真实成功/失败两条路径均不得写 DB、CAS、工作文件或 Git 路径。"""
    before = observe_state(case)
    try:
        return verify_product_git_delivery_core_materials(
            case.cas, case.core if core is None else core, checkpoint=checkpoint
        )
    finally:
        assert observe_state(case) == before


def reject(case, core, code=None):
    with pytest.raises(KernelError) as caught:
        verify(case, core)
    if code is not None:
        assert caught.value.code == code
    assert "新代码" not in str(caught.value)
    assert str(case.workspace_root) not in str(caught.value)
    assert caught.value.__cause__ is None


def rebuild_core(core, **updates):
    """更新普通声明并重算完整 Core，不授予归属或审批。"""
    payload = {
        name: getattr(core, name) for name in type(core).model_fields if name != "fingerprint"
    }
    payload.update(updates)
    return sealed(ProductGitDeliveryCore, payload)


def revalidate_core(payload):
    return ProductGitDeliveryCore.model_validate_json(canonical_bytes(rehash(payload)), strict=True)


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_exact_all_materials_diff_and_commit_are_readonly(
    cas, tmp_path, monkeypatch, fmt, action, platform
):
    """完整真实材料与 Diff 同源；逻辑 Windows 参数不是原生 Windows 验收。"""
    case = make_case(cas, tmp_path, fmt, action, platform)
    observed, original = Counter(), GitMaterialCAS.read

    def read(actual, reference):
        observed[reference.object_id] += 1
        return original(actual, reference)

    def forbidden(*args, **kwargs):
        pytest.fail("材料复核不得持久化或创建业务效果")

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
    for name in ("put_blob", "_put_blob", "save", "transition"):
        monkeypatch.setattr(SQLiteWorkspaceTransactionStore, name, forbidden)
    result = verify(case)
    assert result == case.core and result is not case.core
    assert set(observed) == {node.material.object_id for node in result.object_scope.objects}
    assert not set(observed) & set(result.object_scope.external_history.unique_parent_ids)
    assert all(n >= 1 for n in observed.values())
    assert result.diff_sha256 != case.plan.review_artifact.sha256
    assert not hasattr(result, "approved") and not hasattr(result, "authorize")
    assert not (tmp_path / "worktrees").exists()


@pytest.mark.parametrize("action", ["checkpoint", "commit"])
def test_readonly_reopen_all_full_objects(cas, tmp_path, action):
    case = make_case(cas, tmp_path, action=action)
    with SQLiteWorkspaceTransactionStore(cas.store._root, read_only=True) as reader:
        reopened = replace(case, cas=GitMaterialCAS(reader))
        assert verify(reopened) == case.core


@pytest.mark.parametrize("field,value", [("diff_sha256", ZERO), ("diff_bytes", 1)])
@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_rehashed_wrong_full_diff_is_not_material_success(cas, tmp_path, field, value, fmt):
    case = make_case(cas, tmp_path, fmt)
    core = rebuild_core(case.core, **{field: value})
    reject(case, core, "git_delivery_plan_invalid")


@pytest.mark.parametrize(
    "role", ["base_commit", "base_tree", "target_tree", "tree_member", "delivery_commit"]
)
def test_missing_any_actual_cas_member_is_failure(cas, tmp_path, role):
    case = make_case(cas, tmp_path, action="commit")
    node = next(n for n in case.core.object_scope.objects if role in n.roles)
    # 只移走本测试新建的确切 Blob；不删除用户路径或接触外来验证目录。
    blob = cas.store._blobs / node.material.cas_digest
    assert blob.is_file()
    moved = blob.with_name(blob.name + ".missing")
    blob.rename(moved)
    try:
        reject(case, case.core)
    finally:
        moved.rename(blob)


@pytest.mark.parametrize(
    "role", ["base_commit", "base_tree", "target_tree", "tree_member", "delivery_commit"]
)
def test_corrupt_any_complete_cas_member_is_failure(cas, tmp_path, role):
    case = make_case(cas, tmp_path, action="commit")
    node = next(n for n in case.core.object_scope.objects if role in n.roles)
    blob = cas.store._blobs / node.material.cas_digest
    original = blob.read_bytes()
    blob.write_bytes(b"corrupt-private-body")
    try:
        reject(case, case.core)
    finally:
        blob.write_bytes(original)


def test_valid_unrelated_extra_object_not_accepted_as_scope(cas, tmp_path):
    case = make_case(cas, tmp_path)
    extra = GitObjectMaterial.from_body("blob", "sha256", b"unrelated-private-body")
    reference = cas.persist(extra)
    scope = case.core.object_scope
    node = GitInventoryObject(reference, ("tree_member",), (), None)
    forged = case.core.model_copy(
        update={
            "object_scope": replace(
                scope,
                objects=tuple(sorted((*scope.objects, node), key=lambda n: n.material.object_id)),
            )
        }
    )
    reject(case, forged, "git_delivery_plan_invalid")


@pytest.mark.parametrize("field", ["before-content", "after-mode"])
def test_rehashed_source_change_cannot_diverge_from_complete_target(cas, tmp_path, field):
    case = make_case(cas, tmp_path)
    payload = case.core.model_dump(mode="json")
    baseline, source = payload["baseline"], payload["baseline"]["source"]
    if field == "before-content":
        keep = next(m for m in case.bodies if m.object_type == "blob" and m.body == b"unchanged\n")
        source["mutations"][0]["before"].update(sha256=keep.body_sha256, size=keep.body_bytes)
    else:
        source["mutations"][0]["after"]["mode"] = 0o644
    rehash(source, "digest")
    rehash(baseline, "digest")
    reject(case, revalidate_core(payload))


def test_valid_complete_target_with_unrelated_modification_is_rejected(cas, tmp_path):
    """替换整棵目标及有效全材料仍须拒绝原净变更之外的修改。"""
    case = make_case(cas, tmp_path)
    scope = case.core.object_scope
    target = next(m for m in case.bodies if m.object_id == scope.roots.target_tree.object_id)
    target_node = next(n for n in scope.objects if n.material.object_id == target.object_id)
    child = next(e.child for e in target_node.tree_entries if e.name == b"src")
    child_material = next(m for m in case.bodies if m.object_id == child.object_id)
    changed = GitObjectMaterial.from_body("blob", "sha256", b"unauthorized change\n")
    new_target = _tree("sha256", (("100644", b"keep", changed), ("40000", b"src", child_material)))
    for material in (changed, new_target):
        cas.persist(material)
    bodies = tuple(m for m in case.bodies if m.object_id != target.object_id) + (
        changed,
        new_target,
    )
    base = next(m for m in bodies if m.object_id == scope.roots.base_commit.object_id)
    base_tree = next(m for m in bodies if m.object_id == scope.roots.base_tree.object_id)
    new_scope = _scope(
        cas,
        "sha256",
        "checkpoint",
        "posix",
        base,
        base_tree,
        new_target,
        bodies,
        scope.external_history.parents,
        scope.limits,
    )
    reject(case, rebuild_core(case.core, object_scope=new_scope), "git_delivery_plan_invalid")


@pytest.mark.parametrize(
    "field", ["author_name", "author_email", "message", "raw_commit_sha256", "authored_at"]
)
def test_rehashed_full_commit_spec_still_requires_exact_original_encoding(cas, tmp_path, field):
    case = make_case(cas, tmp_path, action="commit")
    payload = case.core.model_dump(mode="json")
    spec = payload["commit_spec"]
    changes = {
        "author_name": "Other Author",
        "author_email": "other@example.invalid",
        "message": "Other message\n",
        "raw_commit_sha256": ZERO,
        "authored_at": "2026-10-07T08:09:11Z",
    }
    spec[field] = changes[field]
    if field != "raw_commit_sha256":
        payload["call"]["arguments"][field] = changes[field]
    rehash(spec)
    reject(case, revalidate_core(payload), "git_delivery_plan_invalid")


@pytest.mark.parametrize(
    "kind", [TurnCancelled, asyncio.CancelledError, RuntimeError, ValueError, TypeError]
)
@pytest.mark.parametrize("at", [1, 8, 50, 150])
def test_control_exception_at_material_checkpoints_is_preserved(cas, tmp_path, kind, at):
    case = make_case(cas, tmp_path, action="commit")
    failure, calls = kind("parent-checkpoint"), 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == at:
            raise failure

    with pytest.raises(kind) as caught:
        verify(case, checkpoint=checkpoint)
    assert caught.value is failure
    assert calls == at


def test_rehashed_baseline_member_oid_must_match_actual_complete_base_tree(cas, tmp_path):
    case = make_case(cas, tmp_path)
    payload = case.core.model_dump(mode="json")
    keep = next(m for m in case.bodies if m.object_type == "blob" and m.body == b"unchanged\n")
    payload["baseline"]["members"][0]["oid"] = keep.object_id
    rehash(payload["baseline"], "digest")
    reject(case, revalidate_core(payload), "git_delivery_plan_invalid")


@pytest.mark.parametrize(
    "failure",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        ValueError("parent-control"),
        TypeError("parent-control"),
        KernelError("parent_cancel", "原检查点终止"),
    ],
)
def test_cancellation_after_actual_read_is_not_a_partial_success(
    cas, tmp_path, monkeypatch, failure
):
    case = make_case(cas, tmp_path)
    original, read_count = GitMaterialCAS.read, 0

    def read(actual, reference):
        nonlocal read_count
        result = original(actual, reference)
        read_count += 1
        return result

    def checkpoint():
        if read_count:
            raise failure

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    with pytest.raises(type(failure)) as caught:
        verify(case, checkpoint=checkpoint)
    assert caught.value is failure and read_count == 1


@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("part", ["manifest", "chunk"])
def test_full_parent_missing_original_cas_prevents_material_success(cas, tmp_path, action, part):
    """完整 Git 对象仍存在时，任一原 Manifest/Chunk 丢失也不能绕过父历史。"""
    case = make_case(cas, tmp_path, action=action)
    snapshot = case.core.baseline.source.workspace
    manifest = json.loads(cas.store.blob(snapshot.parent_closure.sha256))
    digest = (
        snapshot.parent_closure.sha256 if part == "manifest" else manifest["chunks"][0]["sha256"]
    )
    assert digest not in {node.material.cas_digest for node in case.core.object_scope.objects}
    blob = cas.store._blobs / digest
    moved = blob.with_name(blob.name + ".missing")
    blob.rename(moved)
    try:
        for node in case.core.object_scope.objects:
            assert cas.read(node.material).object_id == node.material.object_id
        reject(case, case.core)
    finally:
        moved.rename(blob)
