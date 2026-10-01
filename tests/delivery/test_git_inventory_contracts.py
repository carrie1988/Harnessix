"""对象目录的严格元数据合同；不使用 CAS、MAC、Owner 或产品写入口。"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields, replace
from uuid import UUID

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_inventory_contracts import (
    GitBaseHistoryBoundary,
    GitInventoryBinding,
    GitInventoryMetrics,
    GitInventoryObject,
    GitInventoryPrefixProjection,
    GitInventoryRoots,
    GitObjectInventory,
    snapshot_git_inventory_prefix_projection,
    snapshot_git_object_inventory,
)
from harnessix.delivery.git_inventory_wire import (
    git_inventory_scope_digest,
    git_object_inventory_digest,
)
from harnessix.delivery.git_material_cas import GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_object_references import parse_git_commit, parse_git_tree
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits


def _check():
    """提供不取消的显式检查点，不创建自己的期限。"""
    return None


def _read(material):
    """从实际材料取得原三字段请求，保留真实格式与 OID。"""
    return GitObjectRead(material.object_type, material.object_id, material.object_format)


def _reference(material):
    """建立原七字段完整正文引用，不附加认证字段。"""
    return GitObjectMaterialReference(
        material.object_type,
        material.object_id,
        material.object_format,
        material.body_sha256,
        material.body_bytes,
        material.body_sha256,
    )


def _tree(object_format, entries):
    """以原二进制 entry 布局构造真实纯 tree 材料。"""
    body = b"".join(
        mode.encode() + b" " + name + b"\0" + bytes.fromhex(child.object_id)
        for mode, name, child in entries
    )
    return GitObjectMaterial.from_body("tree", object_format, body)


def _commit(object_format, tree, parents, message=b"fixture\n"):
    """保留父边原序并构造真实完整 commit 材料。"""
    body = b"tree " + tree.object_id.encode() + b"\n"
    body += b"".join(b"parent " + p.object_id.encode() + b"\n" for p in parents)
    body += b"author A <a@b> 0 +0000\ncommitter A <a@b> 0 +0000\n\n" + message
    return GitObjectMaterial.from_body("commit", object_format, body)


def _seal_shape(value):
    """只重算元数据摘要；名称不表示签发任何 MAC。"""
    scope = git_inventory_scope_digest(value, checkpoint=_check)
    value = replace(value, binding=replace(value.binding, object_scope_digest=scope))
    return replace(value, inventory_sha256=git_object_inventory_digest(value, checkpoint=_check))


def _inventory(
    object_format="sha256", action="checkpoint", phase="materials_ready", platform="posix"
):
    """构造两格式、两动作、两阶段的完整元数据夹具，无实际 CAS 写入。"""
    blob = GitObjectMaterial.from_body("blob", object_format, b"complete fixture\n")
    leaf = _tree(object_format, (("100644", b"a", blob),))
    root = _tree(object_format, (("40000", b"d", leaf), ("40000", b"e", leaf)))
    width = 40 if object_format == "sha1" else 64
    parents = tuple(GitObjectRead("commit", char * width, object_format) for char in "aba")
    base = _commit(object_format, root, parents)
    delivery = _commit(object_format, root, (_read(base),), b"delivery\n")
    materials = [base, root, leaf, blob] + ([delivery] if action == "commit" else [])
    roles = {
        base.object_id: ("base_commit",),
        root.object_id: ("base_tree", "target_tree"),
        leaf.object_id: ("tree_member",),
        blob.object_id: ("tree_member",),
        delivery.object_id: ("delivery_commit",),
    }
    objects = tuple(
        GitInventoryObject(
            _reference(m),
            roles[m.object_id],
            parse_git_tree(m, max_entries=20, checkpoint=_check) if m.object_type == "tree" else (),
            parse_git_commit(m, max_parents=10, checkpoint=_check)
            if m.object_type == "commit"
            else None,
        )
        for m in sorted(materials, key=lambda m: m.object_id)
    )
    binding = GitInventoryBinding(
        *(UUID(int=n) for n in range(1, 10)),
        *("1" * 64 for _ in range(6)),
        "0" * 64,
    )
    previous = "0" * 64
    if phase == "effect_closed":
        previous = _inventory(object_format, action, platform=platform).inventory_sha256
    candidate = GitObjectInventory(
        "harnessix.git-object-inventory/v1",
        UUID(int=10),
        binding,
        action,
        phase,
        0 if phase == "materials_ready" else 1,
        previous,
        platform,
        GitInventoryRoots(
            _read(base), _read(root), _read(root), _read(delivery) if action == "commit" else None
        ),
        objects,
        GitBaseHistoryBoundary(
            "base_commit_parent_edges",
            _read(base),
            parents,
            tuple(sorted({p.object_id for p in parents})),
        ),
        GitTreeClosureLimits(20, 1024 * 1024, 20, 5),
        10,
        GitInventoryMetrics(
            len(objects),
            sum(m.body_bytes for m in materials),
            3,
            3 + (action == "commit"),
            4,
            4,
            1,
            1,
        ),
        "0" * 64,
    )
    return _seal_shape(candidate)


def _reject(value, code="git_inventory_invalid"):
    """要求原快照入口以指定固定领域错误拒绝。"""
    with pytest.raises(KernelError) as error:
        snapshot_git_object_inventory(value, checkpoint=_check)
    assert error.value.code == code


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("phase", ["materials_ready", "effect_closed"])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_all_inventory_shapes_are_deep_snapshots(object_format, action, phase, platform):
    """验证两平台逻辑合同全部组合、深层重建、冻结与重复父边原序。"""
    original = _inventory(object_format, action, phase, platform)
    result = snapshot_git_object_inventory(original, checkpoint=_check)
    assert result == original and result is not original
    assert result.binding is not original.binding
    assert result.objects[0].material is not original.objects[0].material
    assert result.external_history.parents == original.external_history.parents
    assert result.external_history.parents[0] == result.external_history.parents[2]
    with pytest.raises(FrozenInstanceError):
        result.phase = "effect_closed"


@pytest.mark.parametrize("field_name", [f.name for f in fields(GitInventoryBinding)])
def test_binding_actual_types_not_coerced(field_name):
    """绑定字段要求实际类型，不将错误标量转换为合法身份。"""
    value = _inventory()
    object.__setattr__(value.binding, field_name, 1)
    _reject(value)


@pytest.mark.parametrize("field_name", [f.name for f in fields(GitInventoryMetrics)])
@pytest.mark.parametrize("bad", [True, -1, 1.0, "1"])
def test_metrics_actual_integer_contract(field_name, bad):
    """计数只接受非负实际整数，布尔、负数、浮点和字符串均拒绝。"""
    value = _inventory()
    object.__setattr__(value.metrics, field_name, bad)
    _reject(value)


@pytest.mark.parametrize(
    "field_name", ["max_objects", "max_body_bytes", "max_entries", "max_depth"]
)
@pytest.mark.parametrize("bad", [True, -1, 1.0, "1"])
def test_original_limits_are_rebuilt(field_name, bad):
    """原四项限制在深层重建时仍必须是非负实际整数。"""
    value = _inventory()
    object.__setattr__(value.limits, field_name, bad)
    _reject(value, "git_inventory_limit_invalid")


def test_missing_nested_attributes_are_fixed_errors():
    """缺少原引用或限制字段均转为固定领域错误。"""
    value = _inventory()
    object.__delattr__(value.objects[0].material, "body_bytes")
    _reject(value)
    value = _inventory()
    object.__delattr__(value.limits, "max_depth")
    _reject(value, "git_inventory_limit_invalid")


@pytest.mark.parametrize(
    "field_name",
    [
        "object_count",
        "unique_body_bytes",
        "direct_tree_edges",
        "commit_parent_edges",
        "base_expanded_entries",
        "target_expanded_entries",
        "base_tree_depth",
        "target_tree_depth",
    ],
)
def test_declared_metrics_must_equal_metadata_expansion(field_name):
    """声明计数必须等完整元数据并集与逐路径展开的派生值。"""
    value = _inventory()
    object.__setattr__(value.metrics, field_name, getattr(value.metrics, field_name) + 1)
    _reject(value, "git_inventory_graph_mismatch")


@pytest.mark.parametrize(
    "field_name,bad",
    [("domain_sequence", 1), ("phase", "unknown"), ("previous_inventory_sha256", "2" * 64)],
)
def test_stage_and_initial_previous(field_name, bad):
    """阶段、序号和起始零摘要必须满足固定映射。"""
    value = _inventory()
    object.__setattr__(value, field_name, bad)
    _reject(
        value, "git_inventory_stage_mismatch" if field_name != "phase" else "git_inventory_invalid"
    )


def test_objects_must_be_unique_sorted_complete_and_roles_exact():
    """拒绝乱序、重复对象和遗漏真实业务角色的声明。"""
    value = _inventory()
    object.__setattr__(value, "objects", tuple(reversed(value.objects)))
    _reject(value)
    value = _inventory()
    object.__setattr__(value, "objects", (*value.objects, value.objects[-1]))
    _reject(value)
    value = _inventory()
    node = next(o for o in value.objects if "base_tree" in o.roles)
    object.__setattr__(node, "roles", ("base_tree",))
    _reject(value, "git_inventory_graph_mismatch")


def test_parent_order_and_duplicates_cannot_be_replaced_by_membership():
    """历史边界按原父边出现次数比较，不以集合代替序列。"""
    value = _inventory()
    object.__setattr__(value.external_history, "parents", value.external_history.parents[:2])
    _reject(value, "git_inventory_graph_mismatch")


def test_per_path_entries_not_unique_edge_count_and_root_depth_zero():
    """共享子树仍按每条路径展开计预算，根的深度是零。"""
    value = _inventory()
    assert value.metrics.direct_tree_edges == 3
    assert value.metrics.base_expanded_entries == 4
    object.__setattr__(value.limits, "max_entries", 3)
    _reject(value, "git_inventory_limit")


@pytest.mark.parametrize(
    "exception",
    [
        TurnCancelled(),
        asyncio.CancelledError(),
        ValueError("fixture"),
        KernelError("fixture", "固定夹具"),
    ],
)
@pytest.mark.parametrize("at", [1, 20, 70])
def test_checkpoint_exception_object_is_preserved(exception, at):
    """早期及深层检查点保留取消和普通异常的原对象身份。"""
    value = _inventory()
    count = 0

    def check():
        """在原计数检查点抛出指定异常，不替换异常对象。"""
        nonlocal count
        count += 1
        if count == at:
            raise exception

    with pytest.raises(type(exception)) as error:
        snapshot_git_object_inventory(value, checkpoint=check)
    assert error.value is exception


def test_shape_is_not_actual_body_graph_mac_or_ownership_verification():
    """表明重算声明摘要不能证明实际正文、业务 MAC 或跨库归属。"""
    value = _inventory()
    node = next(o for o in value.objects if o.material.object_type == "blob")
    object.__setattr__(node.material, "body_sha256", "9" * 64)
    object.__setattr__(node.material, "cas_digest", "9" * 64)
    value = _seal_shape(value)
    assert snapshot_git_object_inventory(value, checkpoint=_check) == value


def test_prefix_projection_sequence_and_actual_types():
    """普通尾锚投影只检查固定序号和实际标量类型。"""
    value = GitInventoryPrefixProjection(
        UUID(int=1), UUID(int=2), UUID(int=3), 0, 1, "1" * 64, "2" * 64, "3" * 64
    )
    assert snapshot_git_inventory_prefix_projection(value, checkpoint=_check) == value
    object.__setattr__(value, "highest_publication_sequence", 2)
    with pytest.raises(KernelError) as error:
        snapshot_git_inventory_prefix_projection(value, checkpoint=_check)
    assert error.value.code == "git_inventory_stage_mismatch"


@pytest.mark.parametrize("bad", [True, -1, 1.0, "1"])
def test_max_parents_actual_type_is_fixed_limit_error(bad):
    """显式父边预算的坏类型统一为限制输入错误。"""
    value = _inventory()
    object.__setattr__(value, "max_parents", bad)
    _reject(value, "git_inventory_limit_invalid")


@pytest.mark.parametrize(
    "field_name", ["max_objects", "max_body_bytes", "max_entries", "max_depth"]
)
def test_each_explicit_limit_exact_boundary_and_plus_one(field_name):
    """每项限制恰到边界接受，实际观察超一单位拒绝。"""
    value = _inventory()
    exact = {
        "max_objects": value.metrics.object_count,
        "max_body_bytes": value.metrics.unique_body_bytes,
        "max_entries": value.metrics.base_expanded_entries,
        "max_depth": value.metrics.base_tree_depth,
    }[field_name]
    object.__setattr__(value.limits, field_name, exact)
    value = _seal_shape(value)
    assert snapshot_git_object_inventory(value, checkpoint=_check) == value
    object.__setattr__(value.limits, field_name, exact - 1)
    _reject(value, "git_inventory_limit")


def test_closed_previous_equals_reconstructed_same_stream_ready_digest():
    """已闭合阶段前摘要必须等同目录起始阶段完整摘要。"""
    value = _inventory(phase="effect_closed")
    object.__setattr__(value, "previous_inventory_sha256", "f" * 64)
    object.__setattr__(
        value, "inventory_sha256", git_object_inventory_digest(value, checkpoint=_check)
    )
    _reject(value, "git_inventory_stage_mismatch")


@pytest.mark.parametrize("location", ["roots", "object", "entry", "parents", "limits"])
def test_nested_tuple_models_and_actual_classes_not_coerced(location):
    """深层原模型、字节及 tuple 不接受替代容器或恶意子类。"""
    value = _inventory()
    if location == "roots":
        object.__setattr__(value.roots.base_tree, "object_id", 1)
    elif location == "object":
        object.__setattr__(value, "objects", list(value.objects))
    elif location == "entry":
        node = next(o for o in value.objects if o.tree_entries)
        object.__setattr__(node.tree_entries[0], "name", bytearray(b"a"))
    elif location == "parents":
        node = next(o for o in value.objects if o.commit_references)
        object.__setattr__(node.commit_references, "parents", list(node.commit_references.parents))
    else:

        class LimitsSubclass(GitTreeClosureLimits):
            """模拟不能替代原预算模型的子类。"""

            pass

        object.__setattr__(value, "limits", LimitsSubclass(20, 1024, 20, 5))
    _reject(
        value, "git_inventory_limit_invalid" if location == "limits" else "git_inventory_invalid"
    )


def test_tree_binary_order_and_noncontinuous_duplicate_names_rejected():
    """直接引用按原二进制目录排序，并拒绝非连续重复 basename。"""
    value = _inventory()
    node = next(o for o in value.objects if len(o.tree_entries) == 2)
    object.__setattr__(node, "tree_entries", tuple(reversed(node.tree_entries)))
    _reject(value, "git_inventory_graph_mismatch")
    value = _inventory()
    node = next(o for o in value.objects if len(o.tree_entries) == 2)
    object.__setattr__(node, "tree_entries", (*node.tree_entries, node.tree_entries[0]))
    _reject(value, "git_inventory_graph_mismatch")


def test_internal_oid_cannot_be_laundered_as_base_external_history():
    """内部对象不能借基线外部历史边界取得另一种语义。"""
    value = _inventory()
    internal = value.roots.base_commit
    node = next(o for o in value.objects if "base_commit" in o.roles)
    object.__setattr__(node.commit_references, "parents", (internal,))
    object.__setattr__(value.external_history, "parents", (internal,))
    object.__setattr__(value.external_history, "unique_parent_ids", (internal.object_id,))
    _reject(value, "git_inventory_graph_mismatch")


def test_no_optional_verification_context_or_fake_verifier_exposed():
    """未实现权威加载前不提供宽松上下文或伪验真函数。"""
    from harnessix.delivery import git_inventory_contracts

    assert not hasattr(git_inventory_contracts, "GitInventoryVerificationContext")
    assert not hasattr(git_inventory_contracts, "verify_git_object_inventory")


def _models(value):
    """列出七种实际持久模型，统一检验 slots 的固定字段边界。"""
    return (
        value.binding,
        value.roots,
        value.objects[0],
        value.external_history,
        value.metrics,
        value,
        GitInventoryPrefixProjection(
            UUID(int=1), UUID(int=2), UUID(int=3), 0, 1, "1" * 64, "2" * 64, "3" * 64
        ),
    )


@pytest.mark.parametrize("index", range(7))
def test_every_persistent_model_slots_reject_extra_and_missing_fields(index):
    """固定 slots 不容纳额外字段；删除任何字段后快照均给固定领域错误。"""
    model = _models(_inventory())[index]
    with pytest.raises(AttributeError):
        object.__setattr__(model, "extra", "untrusted")
    assert not hasattr(model, "__dict__")
    for member in fields(model):
        value = _inventory()
        models = _models(value)
        object.__delattr__(models[index], member.name)
        if index == 6:
            with pytest.raises(KernelError) as error:
                snapshot_git_inventory_prefix_projection(models[index], checkpoint=_check)
            assert error.value.code == "git_inventory_invalid"
        else:
            _reject(value)


@pytest.mark.parametrize("index", range(7))
def test_malicious_model_subclass_is_rejected_before_attribute_access(index):
    """子类不能靠属性覆盖冒充原模型；拒绝阶段不访问恶意属性。"""
    value = _inventory()
    original = _models(value)[index]
    accessed = []

    def getattribute(_self, name):
        """暴露任何意外子类属性访问，确保拒绝先于访问。"""
        accessed.append(name)
        raise AssertionError("malicious attribute")

    subclass = type("Injected", (type(original),), {"__getattribute__": getattribute})
    untrusted = object.__new__(subclass)
    if index == 6:
        with pytest.raises(KernelError) as error:
            snapshot_git_inventory_prefix_projection(untrusted, checkpoint=_check)
        assert error.value.code == "git_inventory_invalid"
    else:
        if index == 5:
            value = untrusted
        elif index == 2:
            object.__setattr__(value, "objects", (untrusted, *value.objects[1:]))
        else:
            object.__setattr__(
                value, ("binding", "roots", "", "external_history", "metrics")[index], untrusted
            )
        _reject(value)
    assert accessed == []


def test_original_8mib_reference_boundary_is_not_shrunk_or_relaxed():
    """原七字段 CAS 引用完整接受 8MiB 声明，超一字节仍拒绝。"""
    value = _inventory()
    node = next(o for o in value.objects if o.material.object_type == "blob")
    object.__setattr__(node.material, "body_bytes", 8 * 1024 * 1024)
    object.__setattr__(value.limits, "max_body_bytes", 9 * 1024 * 1024)
    object.__setattr__(
        value.metrics, "unique_body_bytes", sum(o.material.body_bytes for o in value.objects)
    )
    value = _seal_shape(value)
    assert snapshot_git_object_inventory(value, checkpoint=_check) == value
    object.__setattr__(node.material, "body_bytes", 8 * 1024 * 1024 + 1)
    _reject(value)


@pytest.mark.parametrize("kind", ["tree", "blob"])
def test_missing_declared_tree_or_blob_is_not_ignored(kind):
    """完整声明图缺 tree 或 blob 时拒绝，不只保留可达子图的部分结果。"""
    value = _inventory()
    removed = next(o for o in value.objects if o.material.object_type == kind)
    object.__setattr__(value, "objects", tuple(o for o in value.objects if o is not removed))
    _reject(value, "git_inventory_missing")


def test_unreachable_catalog_object_cannot_be_adopted_by_role():
    """额外孤儿对象即使自称 tree_member 也不属于完整根并集。"""
    value = _inventory()
    orphan = GitObjectMaterial.from_body("blob", "sha256", b"orphan")
    node = GitInventoryObject(_reference(orphan), ("tree_member",), (), None)
    object.__setattr__(
        value, "objects", tuple(sorted((*value.objects, node), key=lambda o: o.material.object_id))
    )
    _reject(value, "git_inventory_graph_mismatch")


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_declared_paths_reuse_original_windows_comparison_key(platform):
    """目录声明使用原平台路径比较规则；纯元数据正例不是实际 CAS 验真。"""
    value = _inventory(platform=platform)
    root = next(o for o in value.objects if "base_tree" in o.roles)
    object.__setattr__(root.tree_entries[0], "name", b"D")
    object.__setattr__(root.tree_entries[1], "name", b"d")
    if platform == "windows":
        _reject(value, "git_inventory_graph_mismatch")
    else:
        value = _seal_shape(value)
        assert snapshot_git_object_inventory(value, checkpoint=_check) == value


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_business_tree_rejects_links_without_reducing_declared_scope(mode):
    """原 parser 五种模式不变，完整普通业务树仍明确拒绝链接图。"""
    value = _inventory()
    leaf = next(o for o in value.objects if len(o.tree_entries) == 1)
    entry = leaf.tree_entries[0]
    object.__setattr__(entry, "mode", mode)
    if mode == "160000":
        object.__setattr__(entry, "child", value.roots.base_commit)
    _reject(value, "git_inventory_graph_mismatch")


@pytest.mark.parametrize("field_name", ["tree", "parents"])
def test_delivery_commit_cannot_claim_unrelated_tree_or_multiple_base_parents(field_name):
    """交付提交必须精确引用目标树及一条内部基线父边，不授权新历史。"""
    value = _inventory(action="commit")
    node = next(o for o in value.objects if "delivery_commit" in o.roles)
    if field_name == "tree":
        object.__setattr__(node.commit_references, "tree", value.roots.base_commit)
    else:
        object.__setattr__(node.commit_references, "parents", (value.roots.base_commit,) * 2)
    _reject(value, "git_inventory_graph_mismatch")
