"""真实 SQLite CAS 的全目录材料重验；不证明 Owner、账本或业务效果。"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from typing import get_args
from uuid import UUID

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import git_inventory_materials as materials_module
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_inventory_contracts import (
    GitBaseHistoryBoundary,
    GitInventoryBinding,
    GitInventoryMetrics,
    GitInventoryObject,
    GitInventoryRole,
    GitInventoryRoots,
    GitObjectInventory,
    snapshot_git_object_inventory,
)
from harnessix.delivery.git_inventory_materials import verify_git_inventory_materials
from harnessix.delivery.git_inventory_wire import encode_git_object_inventory
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.git_object_references import parse_git_commit, parse_git_tree
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from tests.delivery.test_git_inventory_contracts import (
    _check,
    _commit,
    _read,
    _reference,
    _seal_shape,
    _tree,
)

_BODY = b"synthetic-inventory-body\0\xff"
_FORMATS = ("sha1", "sha256")


@pytest.fixture
def actual_cas(tmp_path):
    """由原 Store 正式创建新私有目录，结束时关闭真实 SQLite 句柄。"""
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


def _nodes(bodies, roots):
    """从完整纯材料装配声明边和全部角色，不使用待验证端口造预期。"""
    entries = {
        m.object_id: parse_git_tree(m, max_entries=5, checkpoint=_check)
        if m.object_type == "tree"
        else ()
        for m in bodies
    }
    roles = {m.object_id: set() for m in bodies}
    for role in ("base_commit", "base_tree", "target_tree", "delivery_commit"):
        root = getattr(roots, role)
        if root is not None:
            roles[root.object_id].add(role)
    for rows in entries.values():
        for entry in rows:
            roles[entry.child.object_id].add("tree_member")
    return tuple(
        GitInventoryObject(
            _reference(m),
            tuple(role for role in get_args(GitInventoryRole) if role in roles[m.object_id]),
            entries[m.object_id],
            parse_git_commit(m, max_parents=3, checkpoint=_check)
            if m.object_type == "commit"
            else None,
        )
        for m in sorted(bodies, key=lambda m: m.object_id)
    )


def _case(
    object_format="sha256",
    action="checkpoint",
    phase="materials_ready",
    platform="posix",
    body=_BODY,
    shared_body=False,
    large_commit=False,
):
    """生成不同两根共享子树及有序重复外部父边的真实完整材料。"""
    blob = GitObjectMaterial.from_body("blob", object_format, body)
    leaf = _tree(object_format, (("100644", b"a", blob),))
    base_tree = _tree(object_format, (("40000", b"old", leaf),))
    width = 40 if object_format == "sha1" else 64
    parents = tuple(GitObjectRead("commit", c * width, object_format) for c in "aba")
    base = _commit(object_format, base_tree, parents)
    if large_commit:
        header = _commit(object_format, base_tree, parents, b"")
        base = _commit(
            object_format,
            base_tree,
            parents,
            b"x" * (MAX_TRANSACTION_FILE_BYTES - header.body_bytes),
        )
    extra = GitObjectMaterial.from_body(
        "blob", object_format, base.body if shared_body else b"target-only"
    )
    target = _tree(
        object_format,
        (("40000", b"d", leaf), ("40000", b"e", leaf), ("100755", b"z", extra)),
    )
    delivery = _commit(object_format, target, (_read(base),), b"delivery\n")
    bodies = (blob, extra, leaf, base_tree, target, base)
    if action == "commit":
        bodies += (delivery,)
    roots = GitInventoryRoots(
        _read(base),
        _read(base_tree),
        _read(target),
        _read(delivery) if action == "commit" else None,
    )
    objects = _nodes(bodies, roots)
    total = sum(m.body_bytes for m in bodies)
    value = GitObjectInventory(
        "harnessix.git-object-inventory/v1",
        UUID(int=20),
        GitInventoryBinding(
            *(UUID(int=n) for n in range(1, 10)), *("1" * 64 for _ in range(6)), "0" * 64
        ),
        action,
        "materials_ready",
        0,
        "0" * 64,
        platform,
        roots,
        objects,
        GitBaseHistoryBoundary(
            "base_commit_parent_edges",
            _read(base),
            parents,
            tuple(sorted({p.object_id for p in parents})),
        ),
        GitTreeClosureLimits(len(objects), total, 5, 1),
        3,
        GitInventoryMetrics(len(objects), total, 5, 3 + (action == "commit"), 2, 5, 1, 1),
        "0" * 64,
    )
    value = _seal_shape(value)
    if phase == "effect_closed":
        value = _seal_shape(
            replace(
                value,
                phase=phase,
                domain_sequence=1,
                previous_inventory_sha256=value.inventory_sha256,
            )
        )
    return value, bodies


def _seed(cas, bodies):
    """只在调用被测只读端口之前由原 CAS 正式持久化完整夹具。"""
    for material in bodies:
        assert cas.persist(material) == _reference(material)


def _state(cas):
    """核对真实所有文件字节摘要和身份，不把只读 atime 当作写入。"""
    return tuple(
        (
            p.relative_to(cas.store._root).as_posix(),
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_ino,
            p.stat().st_mtime_ns,
            p.stat().st_mode,
        )
        for p in sorted(cas.store._root.rglob("*"))
        if p.is_file()
    )


def _verify(cas, value, checkpoint=_check):
    """成功与失败均要求本次端口未改变真实 CAS/DB 文件。"""
    before = _state(cas)
    try:
        return verify_git_inventory_materials(cas, value, checkpoint=checkpoint)
    finally:
        assert _state(cas) == before


def _counts(monkeypatch):
    """仅统计并调用原真实 CAS.read，不以假返回证明材料重验。"""
    counts = Counter()
    original = GitMaterialCAS.read

    def read(cas, reference):
        """保留原返回或异常对象，记录实际 Git OID 的观察次数。"""
        counts[reference.object_id] += 1
        return original(cas, reference)

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    return counts


def _reject(cas, value, code):
    """要求固定错误且没有正文或自由路径泄露，也无部分返回。"""
    with pytest.raises(KernelError) as error:
        _verify(cas, value)
    assert error.value.code == code
    assert _BODY.decode("latin1") not in str(error.value)
    assert error.value.__cause__ is None


def _base_reference(value, reference):
    """替换声明基线引用及同一边界，构造 W0 接受而 CAS 必须拒绝的反例。"""
    base = value.roots.base_commit
    request = GitObjectRead(reference.object_type, reference.object_id, reference.object_format)
    objects = tuple(
        sorted(
            (
                replace(n, material=reference) if n.material.object_id == base.object_id else n
                for n in value.objects
            ),
            key=lambda n: n.material.object_id,
        )
    )
    total = sum(n.material.body_bytes for n in objects)
    return _seal_shape(
        replace(
            value,
            roots=replace(value.roots, base_commit=request),
            external_history=replace(value.external_history, base_commit=request),
            objects=objects,
            metrics=replace(value.metrics, unique_body_bytes=total),
            limits=replace(value.limits, max_body_bytes=total),
        )
    )


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("action", ("checkpoint", "commit"))
@pytest.mark.parametrize("phase", ("materials_ready", "effect_closed"))
@pytest.mark.parametrize("platform", ("posix", "windows"))
def test_real_full_inventory_all_shapes_and_no_external_parent_fetch(
    actual_cas, monkeypatch, object_format, action, phase, platform
):
    """原真实 CAS 全成员及两完整根通过；逻辑 Windows 参数不是原生结果。"""
    value, bodies = _case(object_format, action, phase, platform)
    _seed(actual_cas, bodies)
    counts = _counts(monkeypatch)
    result = _verify(actual_cas, value)
    assert result == value and result is not value
    assert result.objects[0] is not value.objects[0]
    assert result.objects[0].material is not value.objects[0].material
    assert result.external_history.parents == value.external_history.parents
    assert set(counts) == {n.material.object_id for n in value.objects}
    assert counts[value.roots.base_tree.object_id] == 2
    assert counts[value.roots.target_tree.object_id] == 2
    leaf = next(n for n in value.objects if len(n.tree_entries) == 1 and "tree_member" in n.roles)
    assert counts[leaf.material.object_id] == 3
    assert not set(counts) & set(value.external_history.unique_parent_ids)
    assert (result.metrics.base_expanded_entries, result.metrics.target_expanded_entries) == (2, 5)
    assert "body=" not in repr(result) and ", objects=" not in repr(result)
    assert not hasattr(result, "authorize") and not hasattr(result, "mac")
    with pytest.raises(FrozenInstanceError):
        result.phase = "effect_closed"


@pytest.mark.parametrize("object_format", _FORMATS)
def test_same_real_cas_body_distinct_typed_oid_and_roles(actual_cas, monkeypatch, object_format):
    """非空 commit/blob 共用CAS正文，但独立类型OID、角色和每OID正文计数不合并。"""
    value, bodies = _case(object_format, shared_body=True)
    _seed(actual_cas, bodies)
    base = next(n for n in value.objects if "base_commit" in n.roles)
    other = next(
        n
        for n in value.objects
        if n.material.object_type == "blob" and n.material.cas_digest == base.material.cas_digest
    )
    assert base.material.object_id != other.material.object_id and base.roles != other.roles
    assert len({n.material.cas_digest for n in value.objects}) == len(value.objects) - 1
    counts = _counts(monkeypatch)
    assert _verify(actual_cas, value) == value
    assert counts[base.material.object_id] == 1 and counts[other.material.object_id] == 2
    assert value.metrics.unique_body_bytes == sum(n.material.body_bytes for n in value.objects)


@pytest.mark.parametrize(
    "role", ("base_commit", "base_tree", "target_tree", "delivery_commit", "tree_member")
)
def test_real_sqlite_missing_each_member_kind_rejected(actual_cas, role):
    """任何业务根或普通材料缺失不能返回部分目录。"""
    value, bodies = _case(action="commit")
    _seed(actual_cas, bodies)
    node = next(n for n in value.objects if role in n.roles)
    (actual_cas.store._root / "blobs" / node.material.cas_digest).unlink()
    _reject(actual_cas, value, "git_material_cas_read_failed")


@pytest.mark.parametrize("damage", ("same-length", "truncated", "oversize"))
def test_real_corrupt_cas_body_is_original_read_error(actual_cas, damage):
    """真实文件同长损坏、截断和超单体容量均保留原CAS固定码。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    blob = next(m for m in bodies if m.body == _BODY)
    body = b"x" * len(blob.body) if damage == "same-length" else blob.body[:-1]
    if damage == "oversize":
        body = b"x" * (MAX_TRANSACTION_FILE_BYTES + 1)
    (actual_cas.store._root / "blobs" / blob.body_sha256).write_bytes(body)
    _reject(actual_cas, value, "git_material_cas_read_failed")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("field", ("length", "oid", "digest"))
def test_w0_valid_but_actual_seven_field_content_binding_wrong(actual_cas, object_format, field):
    """完整声明摘要正确仍不足以通过真实长度、Git头OID或正文摘要重验。"""
    value, bodies = _case(object_format)
    _seed(actual_cas, bodies)
    reference = next(n.material for n in value.objects if "base_commit" in n.roles)
    changes = {
        "length": {"body_bytes": reference.body_bytes + 1},
        "oid": {"object_id": "f" * len(reference.object_id)},
        "digest": {"body_sha256": "f" * 64, "cas_digest": "f" * 64},
    }
    value = _base_reference(value, replace(reference, **changes[field]))
    assert snapshot_git_object_inventory(value, checkpoint=_check) == value
    _reject(actual_cas, value, "git_material_cas_read_failed")


def _parent_order(value, objects):
    """构造集合未变而顺序错误的基线父边，不省略原有重复次数。"""
    node = next(n for n in objects if "base_commit" in n.roles)
    parents = (
        tuple(reversed(node.commit_references.parents[:2])) + node.commit_references.parents[2:]
    )
    replacement = replace(node, commit_references=replace(node.commit_references, parents=parents))
    value = replace(value, external_history=replace(value.external_history, parents=parents))
    return node, value, replacement


@pytest.mark.parametrize("kind", ("tree-name", "tree-child", "parent-order", "commit-tree"))
def test_w0_valid_declared_direct_references_cannot_replace_actual_parse(actual_cas, kind):
    """虚构 tree 指针和有序父边可通过纯声明，但不能替代实际body引用。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    objects = list(value.objects)
    if kind == "parent-order":
        node, value, replacement = _parent_order(value, objects)
    elif kind == "commit-tree":
        node = next(n for n in objects if "base_commit" in n.roles)
        old = next(n for n in objects if "base_tree" in n.roles)
        target = next(n for n in objects if "target_tree" in n.roles)
        objects.remove(old)
        objects[objects.index(target)] = replace(target, roles=("base_tree", "target_tree"))
        value = replace(
            value,
            roots=replace(value.roots, base_tree=value.roots.target_tree),
            metrics=GitInventoryMetrics(
                len(objects), sum(n.material.body_bytes for n in objects), 4, 3, 5, 5, 1, 1
            ),
        )
        replacement = replace(
            node, commit_references=replace(node.commit_references, tree=value.roots.target_tree)
        )
    else:
        node = next(n for n in objects if "target_tree" in n.roles)
        entries = list(node.tree_entries)
        if kind == "tree-name":
            entries[0] = replace(entries[0], name=b"c")
        else:
            removed = next(
                n for n in objects if n.material.object_id == entries[-1].child.object_id
            )
            common = next(m for m in bodies if m.body == _BODY)
            entries[-1] = replace(entries[-1], child=_read(common))
            objects.remove(removed)
            value = replace(
                value,
                metrics=replace(
                    value.metrics,
                    object_count=len(objects),
                    unique_body_bytes=value.metrics.unique_body_bytes - removed.material.body_bytes,
                ),
            )
        replacement = replace(node, tree_entries=tuple(entries))
    value = _seal_shape(
        replace(value, objects=tuple(replacement if n is node else n for n in objects))
    )
    assert snapshot_git_object_inventory(value, checkpoint=_check) == value
    _reject(actual_cas, value, "git_inventory_materials_mismatch")


def test_real_malformed_commit_is_original_parse_error(actual_cas):
    """实际Git OID/body完整有效不等于commit头结构有效，保留原parser拒绝。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    bad = GitObjectMaterial.from_body("commit", "sha256", b"not-a-commit")
    actual_cas.persist(bad)
    value = _base_reference(value, _reference(bad))
    _reject(actual_cas, value, "git_object_references_invalid")


@pytest.mark.parametrize(
    "field", ("max_objects", "max_body_bytes", "max_entries", "max_depth", "max_parents")
)
def test_original_explicit_limits_exact_and_over_one(actual_cas, field):
    """全夹具恰到每个原限额，低一单位由原W0拒绝，不新建或削减预算。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    assert _verify(actual_cas, value) == value
    if field == "max_parents":
        value = replace(value, max_parents=2)
    else:
        value = replace(
            value, limits=replace(value.limits, **{field: getattr(value.limits, field) - 1})
        )
    _reject(actual_cas, value, "git_inventory_limit")


@pytest.mark.parametrize("object_format", _FORMATS)
@pytest.mark.parametrize("kind", ("blob", "commit"))
def test_actual_complete_8mib_body_not_a_fake_capacity(actual_cas, object_format, kind):
    """真实SQLite目录完整接受8MiB blob或commit，端口读取完整内容而非固定摘要。"""
    body = bytes(range(256)) * (MAX_TRANSACTION_FILE_BYTES // 256) if kind == "blob" else _BODY
    value, bodies = _case(object_format, body=body, large_commit=kind == "commit")
    assert max(m.body_bytes for m in bodies) == MAX_TRANSACTION_FILE_BYTES
    _seed(actual_cas, bodies)
    assert _verify(actual_cas, value) == value


@pytest.mark.parametrize("invalid", ("orphan", "type", "boundary", "role", "oversize-ref", "sha"))
def test_declared_invalid_graph_or_sha_stops_before_any_cas_read(actual_cas, monkeypatch, invalid):
    """先严格snapshot拒绝孤儿/类型/边界/角色/超体引用/SHA，不观察CAS。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    if invalid == "orphan":
        orphan = GitObjectMaterial.from_body("blob", "sha256", b"orphan")
        actual_cas.persist(orphan)
        objects = tuple(
            sorted(
                (
                    *value.objects,
                    GitInventoryObject(_reference(orphan), ("tree_member",), (), None),
                ),
                key=lambda n: n.material.object_id,
            )
        )
        value = replace(
            value,
            objects=objects,
            limits=replace(
                value.limits,
                max_objects=len(objects),
                max_body_bytes=value.limits.max_body_bytes + orphan.body_bytes,
            ),
        )
    elif invalid == "type":
        node = next(n for n in value.objects if n.tree_entries)
        object.__setattr__(node.tree_entries[0].child, "object_type", "commit")
    elif invalid == "boundary":
        object.__setattr__(value.external_history, "parents", value.external_history.parents[:2])
    elif invalid == "role":
        object.__setattr__(value.objects[0], "roles", ("delivery_commit",))
    elif invalid == "oversize-ref":
        object.__setattr__(value.objects[0].material, "body_bytes", MAX_TRANSACTION_FILE_BYTES + 1)
    else:
        object.__setattr__(value, "inventory_sha256", "f" * 64)
    counts = _counts(monkeypatch)
    with pytest.raises(KernelError):
        _verify(actual_cas, value)
    assert not counts


@pytest.mark.parametrize(
    "stage", ("snapshot-first", "tree", "commit", "base", "target", "snapshot-final")
)
@pytest.mark.parametrize(
    "exception",
    (
        TurnCancelled(),
        asyncio.CancelledError(),
        ValueError("fixed"),
        OverflowError("fixed"),
        RecursionError("fixed"),
        KernelError("original_deadline", "原固定期限"),
    ),
)
def test_same_checkpoint_original_exception_identity_all_phases(
    actual_cas, monkeypatch, stage, exception
):
    """统计包装仍调用原snapshot/parse/closure，全部阶段取消原对象无部分返回。"""
    value, bodies = _case(action="commit")
    _seed(actual_cas, bodies)
    active = [None]
    calls = Counter()

    def check():
        """宿主原检查点抛原异常，不新建token或期限。"""
        if active[0] == stage:
            raise exception

    def wrap(name):
        """只标记原调用阶段，不篡改参数、返回或异常对象。"""
        original = getattr(materials_module, name)

        def observed(*args, **kwargs):
            """将同一个原callback传给原API，并在finally恢复统计状态。"""
            assert kwargs["checkpoint"] is check
            calls[name] += 1
            labels = {
                "snapshot_git_object_inventory": ("snapshot-first", "snapshot-final"),
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
                active[0] = previous

        monkeypatch.setattr(materials_module, name, observed)

    for name in (
        "snapshot_git_object_inventory",
        "parse_git_tree",
        "parse_git_commit",
        "verify_git_tree_closure",
    ):
        wrap(name)
    with pytest.raises(type(exception)) as error:
        _verify(actual_cas, value, check)
    assert error.value is exception


def test_frozen_candidate_resnapshot_before_io_disconnects_aliases(actual_cas, monkeypatch):
    """已有canonical冻结候选仍重新snapshot；外部对象后续变更不污染返回内容。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    before = encode_git_object_inventory(value, checkpoint=_check)
    counts = _counts(monkeypatch)

    def check():
        """第一次真实IO后改变外部模型；不触及内部实际CAS内容。"""
        if counts:
            object.__setattr__(value.binding, "source_digest", "f" * 64)

    result = _verify(actual_cas, value, check)
    assert encode_git_object_inventory(result, checkpoint=_check) == before
    assert result.binding is not value.binding


def test_real_readonly_store_zero_sql_and_writer_calls(actual_cas, monkeypatch):
    """读取真正只读SQLite Store；阻止写入口并统计SQL，仅用真实CAS作证明。"""
    value, bodies = _case()
    _seed(actual_cas, bodies)
    counts = Counter()

    def forbidden(*_args, **_kwargs):
        """任何持久写入口均失败，不保存其参数或正文。"""
        counts["write"] += 1
        raise AssertionError("只读端口调用写入口")

    def statement(_text):
        """只计SQL调用数，不保存SQL正文。"""
        counts["sql"] += 1

    monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
    for name in ("put_blob", "save", "transition"):
        monkeypatch.setattr(SQLiteWorkspaceTransactionStore, name, forbidden)
    with SQLiteWorkspaceTransactionStore(actual_cas.store._root, read_only=True) as reader:
        reader._db.set_trace_callback(statement)
        assert _verify(GitMaterialCAS(reader), value) == value
    assert counts == Counter()


def test_exact_cas_port_not_fake_or_subclass(actual_cas):
    """不会以Fake CAS或宽松子类代替原真实读取合同。"""
    value, _ = _case()

    class OtherCAS(GitMaterialCAS):
        """只有类型差异的拒绝输入，不用于成功证明。"""

    for cas in (None, object(), OtherCAS(actual_cas.store)):
        with pytest.raises(KernelError) as error:
            verify_git_inventory_materials(cas, value, checkpoint=_check)
        assert error.value.code == "git_inventory_materials_invalid"
