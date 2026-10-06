"""无绑定 scope 的真实 CAS 全图只读验证、失败传播与旧目录共享算法回归。"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_inventory_materials as materials_module
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_inventory_contracts import (
    GitInventoryScope,
    snapshot_git_inventory_scope,
)
from harnessix.delivery.git_inventory_materials import (
    verify_git_inventory_materials,
    verify_git_inventory_scope_materials,
)
from harnessix.delivery.git_inventory_wire import encode_git_object_inventory
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial
from tests.delivery import test_git_inventory_materials_capacity as capacity_cases
from tests.delivery.test_git_inventory_contracts import _check, _reference
from tests.delivery.test_git_inventory_materials import (
    _base_reference,
    _case,
    _counts,
    _parent_order,
    _seed,
    _state,
)
from tests.delivery.test_git_inventory_materials import (
    actual_cas as actual_cas,
)
from tests.support.git_inventory_scope_cases import (
    from_inventory,
    material_scope,
    metadata_scope,
    verify_unchanged,
)


def _reject(cas, scope, code):
    """真实原 CAS 错误保持固定码及无部分返回，读取前后不改持久文件。"""
    with pytest.raises(KernelError) as raised:
        verify_unchanged(cas, scope)
    assert raised.value.code == code and raised.value.__cause__ is None


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("action", ("checkpoint", "commit"))
@pytest.mark.parametrize("platform", ("posix", "windows"))
def test_real_complete_scope_all_shapes_reuses_full_cas_reads(
    actual_cas, monkeypatch, object_format, action, platform
):
    """实际两树、全部类型根和重复父边通过，不读取外部历史或省略共享子树。"""
    scope, bodies = material_scope(object_format, action, platform)
    _seed(actual_cas, bodies)
    counts = _counts(monkeypatch)
    result = verify_unchanged(actual_cas, scope)
    assert type(result) is GitInventoryScope and result == scope and result is not scope
    assert result.objects[0] is not scope.objects[0]
    assert result.objects[0].material is not scope.objects[0].material
    assert set(counts) == {node.material.object_id for node in scope.objects}
    assert counts[scope.roots.base_tree.object_id] == 2
    assert counts[scope.roots.target_tree.object_id] == 2
    leaf = next(
        node
        for node in scope.objects
        if len(node.tree_entries) == 1 and "tree_member" in node.roles
    )
    assert counts[leaf.material.object_id] == 3
    assert not set(counts) & set(scope.external_history.unique_parent_ids)
    assert result.external_history.parents == scope.external_history.parents
    assert len(result.external_history.parents) == 3


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
def test_same_cas_body_keeps_distinct_typed_objects_and_roles(
    actual_cas, monkeypatch, object_format
):
    """commit/blob 共用正文也分别保留完整 OID、类型、角色及正文计数。"""
    scope, bodies = material_scope(object_format, shared_body=True)
    _seed(actual_cas, bodies)
    base = next(node for node in scope.objects if "base_commit" in node.roles)
    other = next(
        node
        for node in scope.objects
        if node.material.object_type == "blob"
        and node.material.cas_digest == base.material.cas_digest
    )
    assert base.material.object_id != other.material.object_id
    assert base.roles != other.roles
    counts = _counts(monkeypatch)
    assert verify_unchanged(actual_cas, scope) == scope
    assert counts[base.material.object_id] == 1 and counts[other.material.object_id] == 2
    assert scope.metrics.unique_body_bytes == sum(
        node.material.body_bytes for node in scope.objects
    )


@pytest.mark.parametrize(
    "role", ("base_commit", "base_tree", "target_tree", "delivery_commit", "tree_member")
)
def test_missing_real_cas_member_of_every_role_is_rejected(actual_cas, role):
    """任何根或普通叶的真实文件缺失都不能返回部分 scope。"""
    scope, bodies = material_scope(action="commit")
    _seed(actual_cas, bodies)
    node = next(node for node in scope.objects if role in node.roles)
    (actual_cas.store._root / "blobs" / node.material.cas_digest).unlink()
    _reject(actual_cas, scope, "git_material_cas_read_failed")


@pytest.mark.parametrize("damage", ("same-length", "truncated", "oversize"))
def test_actual_cas_corruption_preserves_original_read_error(actual_cas, damage):
    """同长篡改、截断和超过原 8MiB 的真实文件均保留原 CAS 错误。"""
    scope, bodies = material_scope()
    _seed(actual_cas, bodies)
    node = next(node for node in scope.objects if node.material.object_type == "blob")
    original = actual_cas.read(node.material).body
    bad = b"x" * len(original) if damage == "same-length" else original[:-1]
    if damage == "oversize":
        bad = b"x" * (MAX_TRANSACTION_FILE_BYTES + 1)
    (actual_cas.store._root / "blobs" / node.material.cas_digest).write_bytes(bad)
    _reject(actual_cas, scope, "git_material_cas_read_failed")


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("field", ("length", "oid", "digest"))
def test_pure_graph_cannot_replace_actual_seven_field_binding(actual_cas, object_format, field):
    """纯声明通过仍不能代替真实正文长度、SHA256 和带类型 Git OID 验真。"""
    inventory, bodies = _case(object_format)
    _seed(actual_cas, bodies)
    reference = next(node.material for node in inventory.objects if "base_commit" in node.roles)
    changes = {
        "length": {"body_bytes": reference.body_bytes + 1},
        "oid": {"object_id": "f" * len(reference.object_id)},
        "digest": {"body_sha256": "f" * 64, "cas_digest": "f" * 64},
    }[field]
    scope = from_inventory(_base_reference(inventory, replace(reference, **changes)))
    assert snapshot_git_inventory_scope(scope, checkpoint=_check) == scope
    _reject(actual_cas, scope, "git_material_cas_read_failed")


@pytest.mark.parametrize("kind", ("tree-name", "parent-order"))
def test_pure_declared_references_do_not_replace_actual_body_parse(actual_cas, kind):
    """保留完整集合但虚构目录名或父边原序，实际 CAS 解析仍拒绝。"""
    scope, bodies = material_scope()
    _seed(actual_cas, bodies)
    if kind == "parent-order":
        node, scope, replacement = _parent_order(scope, scope.objects)
    else:
        node = next(node for node in scope.objects if "target_tree" in node.roles)
        replacement = replace(
            node, tree_entries=(replace(node.tree_entries[0], name=b"c"), *node.tree_entries[1:])
        )
    scope = replace(
        scope, objects=tuple(replacement if item is node else item for item in scope.objects)
    )
    assert snapshot_git_inventory_scope(scope, checkpoint=_check) == scope
    _reject(actual_cas, scope, "git_inventory_materials_mismatch")


def test_real_malformed_commit_preserves_original_parser_error(actual_cas):
    """真实完整正文和 OID 不保证 commit 头合法，不转换原解析错误。"""
    inventory, bodies = _case()
    _seed(actual_cas, bodies)
    bad = GitObjectMaterial.from_body("commit", "sha256", b"not-a-commit")
    actual_cas.persist(bad)
    scope = from_inventory(_base_reference(inventory, _reference(bad)))
    _reject(actual_cas, scope, "git_object_references_invalid")


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("kind", ("blob", "commit"))
def test_scope_reads_real_complete_8mib_object(actual_cas, object_format, kind):
    """两格式真实完整 8MiB blob/commit 均读取全部字节，不扩大原单体限额。"""
    options = (
        {"body": bytes(range(256)) * (MAX_TRANSACTION_FILE_BYTES // 256)}
        if kind == "blob"
        else {"large_commit": True}
    )
    scope, bodies = material_scope(object_format, **options)
    assert max(material.body_bytes for material in bodies) == MAX_TRANSACTION_FILE_BYTES
    _seed(actual_cas, bodies)
    assert verify_unchanged(actual_cas, scope) == scope


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("platform", ("posix", "windows"))
def test_scope_reads_real_complete_8mib_tree_without_new_fixture_algorithm(
    actual_cas, monkeypatch, object_format, platform
):
    """复用原完整大树容量夹具，并用真实两个入口分别验证全部叶路径。"""
    calls = []

    def verify_both(cas, inventory, *, checkpoint):
        """保持原验证结果，额外验证同一真实 CAS 的无绑定完整投影。"""
        original = verify_git_inventory_materials(cas, inventory, checkpoint=checkpoint)
        scope = from_inventory(inventory)
        observed = verify_unchanged(cas, scope, checkpoint)
        assert observed == from_inventory(original)
        calls.append(observed)
        return original

    monkeypatch.setattr(capacity_cases, "verify_git_inventory_materials", verify_both)
    capacity_cases.test_actual_complete_8mib_tree_inventory(actual_cas, object_format, platform)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "name", ("max_objects", "max_body_bytes", "max_entries", "max_depth", "max_parents")
)
def test_explicit_scope_limits_reject_before_cas_read(actual_cas, monkeypatch, name):
    """预算精确边界通过；缩小任一预算即拒绝，不开始 CAS 读取。"""
    scope, bodies = material_scope()
    _seed(actual_cas, bodies)
    assert verify_unchanged(actual_cas, scope) == scope
    if name == "max_parents":
        scope = replace(scope, max_parents=scope.max_parents - 1)
    else:
        scope = replace(
            scope, limits=replace(scope.limits, **{name: getattr(scope.limits, name) - 1})
        )
    counts = _counts(monkeypatch)
    _reject(actual_cas, scope, "git_inventory_limit")
    assert not counts


@pytest.mark.parametrize("kind", ("inventory", "dict", "bool", "extra-fields", "metrics", "ref"))
def test_bad_scope_stops_before_cas_read(actual_cas, monkeypatch, kind):
    """严格深层拒绝先于材料观察，原业务模型不自动降级为无绑定 scope。"""
    scope, bodies = material_scope()
    _seed(actual_cas, bodies)
    if kind == "inventory":
        scope = _case()[0]
    elif kind in {"dict", "extra-fields"}:
        scope = {name: getattr(scope, name) for name in GitInventoryScope.__slots__}
        if kind == "extra-fields":
            scope["binding"] = "untrusted"
    elif kind == "bool":
        object.__setattr__(scope, "max_parents", True)
    elif kind == "metrics":
        object.__setattr__(scope.metrics, "object_count", True)
    else:
        object.__setattr__(scope.objects[0].material, "body_bytes", MAX_TRANSACTION_FILE_BYTES + 1)
    counts = _counts(monkeypatch)
    with pytest.raises(KernelError):
        verify_unchanged(actual_cas, scope)
    assert not counts


@pytest.mark.parametrize("kind", ("object", "subclass"))
def test_scope_requires_original_actual_cas_type(actual_cas, kind):
    """材料端口仍要求原实际 CAS 类型，不接受可冒充读取函数的子类。"""
    if kind == "subclass":

        class InjectedCAS(GitMaterialCAS):
            """仅用作拒绝反例，不替代真实 CAS 验证。"""

        port = InjectedCAS(actual_cas.store)
    else:
        port = object()
    with pytest.raises(KernelError) as raised:
        verify_git_inventory_scope_materials(port, metadata_scope(), checkpoint=_check)
    assert raised.value.code == "git_inventory_materials_invalid"


@pytest.mark.parametrize(
    "stage",
    ("entry", "snapshot-first", "tree", "commit", "base", "target", "snapshot-final", "final"),
)
@pytest.mark.parametrize("kind", ("turn", "task", "deadline", "callback"))
def test_all_scope_phases_keep_checkpoint_and_original_exception_identity(
    actual_cas, monkeypatch, stage, kind
):
    """初末检查、原快照、解析器与两树 closure 均用同一宿主检查点。"""
    scope, bodies = material_scope(action="commit")
    _seed(actual_cas, bodies)
    active = ["entry"]
    calls = Counter()
    error = {
        "turn": TurnCancelled(),
        "task": asyncio.CancelledError(),
        "deadline": KernelError("original_deadline", "原固定期限"),
        "callback": ValueError("fixed"),
    }[kind]

    def check():
        """抛原异常对象，不自行设置预算、token 或新的期限。"""
        if active[0] == stage:
            raise error

    def wrap(name):
        """统计并调用原验证端口，不制造替代图或材料结果。"""
        original = getattr(materials_module, name)

        def observed(*args, **kwargs):
            """原检查点从首尾到嵌套算法保持身份与错误传播。"""
            assert kwargs["checkpoint"] is check
            calls[name] += 1
            labels = {
                "snapshot_git_inventory_scope": ("snapshot-first", "snapshot-final"),
                "verify_git_tree_closure": ("base", "target"),
            }
            previous = active[0]
            active[0] = (
                labels[name][calls[name] - 1]
                if name in labels
                else ("tree" if name == "parse_git_tree" else "commit")
            )
            try:
                return original(*args, **kwargs)
            finally:
                active[0] = (
                    "final"
                    if name == "snapshot_git_inventory_scope" and calls[name] == 2
                    else previous
                )

        monkeypatch.setattr(materials_module, name, observed)

    for name in (
        "snapshot_git_inventory_scope",
        "parse_git_tree",
        "parse_git_commit",
        "verify_git_tree_closure",
    ):
        wrap(name)
    with pytest.raises(type(error)) as raised:
        verify_unchanged(actual_cas, scope, check)
    assert raised.value is error


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
@pytest.mark.parametrize("action", ("checkpoint", "commit"))
@pytest.mark.parametrize("phase", ("materials_ready", "effect_closed"))
def test_original_inventory_and_scope_share_actual_algorithm_preserve_original_wire(
    actual_cas, monkeypatch, object_format, action, phase
):
    """共享读取算法不丢原阶段、绑定和规范字节；同一实际材料两端结果一致。"""
    inventory, bodies = _case(object_format, action, phase)
    _seed(actual_cas, bodies)
    before = _state(actual_cas)
    original_body = encode_git_object_inventory(inventory, checkpoint=_check)
    counts = _counts(monkeypatch)
    result = verify_git_inventory_materials(actual_cas, inventory, checkpoint=_check)
    original_counts = counts.copy()
    counts.clear()
    scope = from_inventory(inventory)
    scope_result = verify_unchanged(actual_cas, scope)
    assert counts == original_counts
    assert scope_result == from_inventory(result)
    assert type(result) is type(inventory) and result == inventory
    assert encode_git_object_inventory(result, checkpoint=_check) == original_body
    assert _state(actual_cas) == before
