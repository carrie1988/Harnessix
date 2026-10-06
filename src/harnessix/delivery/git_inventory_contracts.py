"""Git 业务目录的严格元数据合同；不读取 CAS、不验业务 MAC 或执行归属。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from types import UnionType
from typing import Literal, cast, get_args, get_origin, get_type_hints
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_material_cas import GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.delivery.git_object_references import GitCommitReferences, GitTreeEntry
from harnessix.delivery.git_tree_closure import GitTreeClosureLimits
from harnessix.workspace.contracts import PlatformKind
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key

GitInventoryRole = Literal[
    "base_commit", "base_tree", "target_tree", "tree_member", "delivery_commit"
]
_ROLES = get_args(GitInventoryRole)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MODES = {"40000": "tree", "100644": "blob", "100755": "blob", "120000": "blob", "160000": "commit"}


def _invalid(code: str = "git_inventory_invalid") -> KernelError:
    """生成不含目录正文或调用方字段的固定领域错误。"""
    return KernelError(code, "Git对象目录元数据不符合持久契约")


class _Contract:
    """为有限目录模型共用局部构造检查，不提供验真上下文。"""

    __slots__ = ()

    def __post_init__(self) -> None:
        """检查直接字段；深层重建与声明图校验由持久入口负责。"""
        # 构造只作局部形状检查；持久入口必须完整深层 snapshot 及摘要检查。
        _local(self)


@dataclass(frozen=True, slots=True)
class GitInventoryBinding(_Contract):
    """保存原宿主绑定字段；UUID 和摘要本身不证明权威归属。"""

    store_id: UUID
    key_id: UUID
    delivery_id: UUID
    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    route_id: UUID
    publication_epoch: UUID
    binding_epoch: UUID
    route_fingerprint: str
    product_plan_fingerprint: str
    implementation_digest: str
    source_digest: str
    baseline_digest: str
    recipe_digest: str
    object_scope_digest: str


@dataclass(frozen=True, slots=True)
class GitInventoryRoots(_Contract):
    """声明完整基线、目标树及可选交付提交的四个类型根。"""

    base_commit: GitObjectRead
    base_tree: GitObjectRead
    target_tree: GitObjectRead
    delivery_commit: GitObjectRead | None


@dataclass(frozen=True, slots=True)
class GitInventoryObject(_Contract):
    """以原七字段 CAS 引用保存一个对象及其全部业务角色、直接引用。"""

    material: GitObjectMaterialReference
    roles: tuple[GitInventoryRole, ...]
    tree_entries: tuple[GitTreeEntry, ...] = field(repr=False)
    commit_references: GitCommitReferences | None


@dataclass(frozen=True, slots=True)
class GitBaseHistoryBoundary(_Contract):
    """保留基线提交父边的原顺序和重复出现，不授权外部历史读取。"""

    kind: Literal["base_commit_parent_edges"]
    base_commit: GitObjectRead
    parents: tuple[GitObjectRead, ...]
    unique_parent_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitInventoryMetrics(_Contract):
    """保存唯一对象量与两根逐路径展开量，避免对象去重省略路径预算。"""

    object_count: int
    unique_body_bytes: int
    direct_tree_edges: int
    commit_parent_edges: int
    base_expanded_entries: int
    target_expanded_entries: int
    base_tree_depth: int
    target_tree_depth: int


@dataclass(frozen=True, slots=True)
class GitInventoryScope(_Contract):
    """规划前的完整对象图声明；不包含身份、阶段、批准或认证摘要。"""

    action_kind: Literal["checkpoint", "commit"]
    platform: PlatformKind
    roots: GitInventoryRoots
    objects: tuple[GitInventoryObject, ...] = field(repr=False)
    external_history: GitBaseHistoryBoundary
    limits: GitTreeClosureLimits
    max_parents: int
    metrics: GitInventoryMetrics


@dataclass(frozen=True, slots=True)
class GitObjectInventory(_Contract):
    """保存单阶段完整目录声明；通过形状检查不代表实际 CAS 或业务验真。"""

    spec_version: Literal["harnessix.git-object-inventory/v1"]
    inventory_id: UUID
    binding: GitInventoryBinding
    action_kind: Literal["checkpoint", "commit"]
    phase: Literal["materials_ready", "effect_closed"]
    domain_sequence: int
    previous_inventory_sha256: str
    platform: PlatformKind
    roots: GitInventoryRoots
    objects: tuple[GitInventoryObject, ...] = field(repr=False)
    external_history: GitBaseHistoryBoundary
    limits: GitTreeClosureLimits
    max_parents: int
    metrics: GitInventoryMetrics
    inventory_sha256: str


@dataclass(frozen=True, slots=True)
class GitInventoryPrefixProjection(_Contract):
    """尾锚的普通子投影；构造成功不证明任何前缀或 MAC。"""

    inventory_id: UUID
    delivery_id: UUID
    publication_epoch: UUID
    highest_domain_sequence: int
    highest_publication_sequence: int
    inventory_sha256: str
    record_body_sha256: str
    prefix_sha256: str


type _KnownModel = (
    GitInventoryBinding
    | GitInventoryRoots
    | GitInventoryObject
    | GitBaseHistoryBoundary
    | GitInventoryMetrics
    | GitInventoryScope
    | GitObjectInventory
    | GitInventoryPrefixProjection
    | GitObjectMaterialReference
    | GitObjectRead
    | GitTreeEntry
    | GitCommitReferences
    | GitTreeClosureLimits
)
_TYPES: set[type[_KnownModel]] = {
    GitInventoryBinding,
    GitInventoryRoots,
    GitInventoryObject,
    GitBaseHistoryBoundary,
    GitInventoryMetrics,
    GitInventoryScope,
    GitObjectInventory,
    GitInventoryPrefixProjection,
    GitObjectMaterialReference,
    GitObjectRead,
    GitTreeEntry,
    GitCommitReferences,
    GitTreeClosureLimits,
}


_ANNOTATIONS: dict[type[object], dict[str, object]] = {}


def _annotations(kind: type[object]) -> dict[str, object]:
    """只解析有限白名单模型的字段类型，缓存不接受调用方扩展。"""
    if kind not in _TYPES:
        raise _invalid()
    if kind not in _ANNOTATIONS:
        _ANNOTATIONS[kind] = cast(dict[str, object], get_type_hints(kind))
    return _ANNOTATIONS[kind]


def _outer(value: object, annotation: object) -> bool:
    """判断实际外层类型及 Literal，拒绝隐式转换和模型子类。"""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return any(type(value) is type(item) and value == item for item in args)
    if origin is UnionType:
        return any(_outer(value, item) for item in args)
    return type(value) is (origin or annotation)


def _local_fields(value: object, kind: type[object]) -> None:
    """检查有限模型的直接字段实际类型、非负整数与摘要格式。"""
    for name, annotation in _annotations(kind).items():
        try:
            member: object = getattr(value, name)
        except AttributeError:
            raise _invalid() from None
        if name == "max_parents" and (type(member) is not int or member < 0):
            raise _invalid("git_inventory_limit_invalid")
        if not _outer(member, annotation):
            raise _invalid()
        if type(member) is int and member < 0:
            raise _invalid(
                "git_inventory_limit_invalid" if name == "max_parents" else "git_inventory_invalid"
            )
        if type(member) is str and (
            name.endswith("digest") or name.endswith("sha256") or name.endswith("fingerprint")
        ):
            if _DIGEST.fullmatch(member) is None:
                raise _invalid()


def _local(value: object) -> None:
    """检查局部字段及模型特有的角色次序与阶段序号。"""
    kind = type(value)
    if kind not in _TYPES:
        raise _invalid()
    _local_fields(value, kind)
    if kind is GitInventoryObject:
        roles = cast(GitInventoryObject, value).roles
        if (
            not roles
            or any(type(role) is not str or role not in _ROLES for role in roles)
            or roles != tuple(role for role in _ROLES if role in roles)
        ):
            raise _invalid()
    elif kind is GitObjectInventory:
        inventory = cast(GitObjectInventory, value)
        if inventory.domain_sequence != (0 if inventory.phase == "materials_ready" else 1) or (
            inventory.domain_sequence == 0
        ) != (inventory.previous_inventory_sha256 == "0" * 64):
            raise _invalid("git_inventory_stage_mismatch")
    elif kind is GitInventoryPrefixProjection:
        projection = cast(GitInventoryPrefixProjection, value)
        if (
            projection.highest_domain_sequence not in {0, 1}
            or projection.highest_publication_sequence != projection.highest_domain_sequence + 1
        ):
            raise _invalid("git_inventory_stage_mismatch")


def _snapshot_field(value: object, annotation: object, checkpoint: Callable[[], None]) -> object:
    """深层重建一个字段，逐元素执行原检查点且不捕获回调异常。"""
    checkpoint()
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        for alternative in args:
            if _outer(value, alternative):
                return _snapshot_field(value, alternative, checkpoint)
        raise _invalid()
    if not _outer(value, annotation):
        code = "git_inventory_limit_invalid" if annotation is GitTreeClosureLimits else None
        raise _invalid(code or "git_inventory_invalid")
    if origin is tuple:
        return tuple(
            _snapshot_field(item, args[0], checkpoint) for item in cast(tuple[object, ...], value)
        )
    if annotation in _TYPES:
        return _snapshot_model(value, annotation, checkpoint)
    return value


def _snapshot_model[T: _KnownModel](
    value: object, kind: type[T], checkpoint: Callable[[], None]
) -> T:
    """拒绝缺字段及子类，调用原构造器重建白名单模型的完整实际字段。"""
    checkpoint()
    code = (
        "git_inventory_limit_invalid" if kind is GitTreeClosureLimits else "git_inventory_invalid"
    )
    if type(value) is not kind:
        raise _invalid(code)
    rebuilt: dict[str, object] = {}
    for name, annotation in _annotations(kind).items():
        checkpoint()
        try:
            member: object = getattr(value, name)
        except AttributeError:
            raise _invalid(code) from None
        if (kind is GitTreeClosureLimits or name == "max_parents") and (
            type(member) is not int or member < 0
        ):
            raise _invalid("git_inventory_limit_invalid")
        rebuilt[name] = _snapshot_field(member, annotation, checkpoint)
    # 回调必须位于构造异常映射之外，不捕获业务取消或自有回调异常。
    checkpoint()
    try:
        result = cast(Callable[..., T], kind)(**rebuilt)
    except KernelError as error:
        if kind in {GitObjectRead, GitObjectMaterialReference, GitTreeClosureLimits}:
            raise _invalid(code) from None
        raise error
    if kind is GitObjectRead:
        request = cast(GitObjectRead, result)
        if request.object_id == "0" * len(request.object_id):
            raise _invalid()
    checkpoint()
    return result


def _request(reference: GitObjectMaterialReference) -> GitObjectRead:
    """从已重建的七字段引用取原类型、OID 与格式，不引入批准字段。"""
    return GitObjectRead(reference.object_type, reference.object_id, reference.object_format)


def _node_shape(node: GitInventoryObject, checkpoint: Callable[[], None]) -> None:
    """校验声明直接引用、二进制排序、非连续重复名及 tree 编码长度。"""
    kind = node.material.object_type
    if (kind != "tree" and node.tree_entries) or (kind == "commit") != (
        node.commit_references is not None
    ):
        raise _invalid("git_inventory_graph_mismatch")
    previous: bytes | None = None
    names: set[bytes] = set()
    length = 0
    for entry in node.tree_entries:
        checkpoint()
        name = entry.name
        if (
            not name
            or name in {b".", b".."}
            or name.lower() == b".git"
            or b"/" in name
            or b"\0" in name
            or name in names
            or entry.child.object_type != _MODES[entry.mode]
        ):
            raise _invalid("git_inventory_graph_mismatch")
        key = name + (b"/" if entry.mode == "40000" else b"\0")
        if previous is not None and key <= previous:
            raise _invalid("git_inventory_graph_mismatch")
        previous = key
        names.add(name)
        length += len(entry.mode) + len(name) + 2 + len(entry.child.object_id) // 2
    if kind == "tree" and length != node.material.body_bytes:
        raise _invalid("git_inventory_graph_mismatch")


def _member(request: GitObjectRead, catalog: dict[str, GitInventoryObject]) -> GitInventoryObject:
    """要求内部类型引用存在且三字段完全一致，不读取或补充 CAS。"""
    node = catalog.get(request.object_id)
    if node is None:
        raise _invalid("git_inventory_missing")
    if _request(node.material) != request:
        raise _invalid("git_inventory_graph_mismatch")
    return node


def _walk(
    root: GitObjectRead,
    catalog: dict[str, GitInventoryObject],
    platform: PlatformKind,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> tuple[int, int, set[str]]:
    """只遍历声明的元数据；不能称为原 CAS 全图验真。"""
    pending: list[tuple[GitObjectRead, str, int, tuple[str, ...]]] = [(root, "", 0, ())]
    paths: set[str] = set()
    reachable: set[str] = set()
    count, maximum_depth = 0, 0
    while pending:
        checkpoint()
        request, prefix, depth, ancestors = pending.pop()
        if depth > limits.max_depth:
            raise _invalid("git_inventory_limit")
        if request.object_id in ancestors:
            raise _invalid("git_inventory_graph_mismatch")
        node = _member(request, catalog)
        reachable.add(request.object_id)
        count += len(node.tree_entries)
        if count > limits.max_entries:
            raise _invalid("git_inventory_limit")
        maximum_depth = max(maximum_depth, depth)
        for entry in reversed(node.tree_entries):
            checkpoint()
            try:
                path = (prefix + "/" if prefix else "") + entry.name.decode("utf-8", "strict")
                if normalize_workspace_path(path, platform) != path:
                    raise ValueError
                key = path_comparison_key(path, platform)
            except (UnicodeError, ValueError, KernelError):
                raise _invalid("git_inventory_graph_mismatch") from None
            if key in paths or entry.mode not in {"40000", "100644", "100755"}:
                raise _invalid("git_inventory_graph_mismatch")
            paths.add(key)
            _member(entry.child, catalog)
            if entry.mode == "40000":
                pending.append((entry.child, path, depth + 1, (*ancestors, request.object_id)))
            else:
                reachable.add(entry.child.object_id)
    checkpoint()
    return count, maximum_depth, reachable


def _root_shape(
    value: GitObjectInventory | GitInventoryScope, catalog: dict[str, GitInventoryObject]
) -> None:
    """核对四根、唯一外部父边边界及交付提交的精确内部父引用。"""
    roots = value.roots
    if (
        roots.base_commit.object_type != "commit"
        or roots.base_tree.object_type != "tree"
        or roots.target_tree.object_type != "tree"
        or (value.action_kind == "commit") != (roots.delivery_commit is not None)
        or (roots.delivery_commit is not None and roots.delivery_commit.object_type != "commit")
    ):
        raise _invalid("git_inventory_graph_mismatch")
    base = _member(roots.base_commit, catalog).commit_references
    boundary = value.external_history
    if (
        base is None
        or base.tree != roots.base_tree
        or boundary.base_commit != roots.base_commit
        or boundary.parents != base.parents
        or boundary.unique_parent_ids != tuple(sorted({p.object_id for p in base.parents}))
        or set(boundary.unique_parent_ids) & catalog.keys()
    ):
        raise _invalid("git_inventory_graph_mismatch")
    if roots.delivery_commit is not None:
        delivery = _member(roots.delivery_commit, catalog).commit_references
        if (
            delivery is None
            or delivery.tree != roots.target_tree
            or delivery.parents != (roots.base_commit,)
        ):
            raise _invalid("git_inventory_graph_mismatch")


def _inventory_catalog(
    value: GitObjectInventory | GitInventoryScope,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> tuple[dict[str, GitInventoryObject], dict[str, set[GitInventoryRole]], int, int, int]:
    """构建严格有序声明目录，核对直接引用并累计唯一正文和直接边。"""
    catalog: dict[str, GitInventoryObject] = {}
    roles: dict[str, set[GitInventoryRole]] = {}
    total_bytes, tree_edges, parent_edges = 0, 0, 0
    for node in value.objects:
        checkpoint()
        oid = node.material.object_id
        if oid in catalog or (catalog and oid <= next(reversed(catalog))):
            raise _invalid()
        if node.material.object_format != value.roots.base_commit.object_format:
            raise _invalid("git_inventory_graph_mismatch")
        _node_shape(node, checkpoint)
        catalog[oid], roles[oid] = node, set()
        total_bytes += node.material.body_bytes
        tree_edges += len(node.tree_entries)
        if node.commit_references is not None:
            refs = node.commit_references
            if len(refs.parents) > value.max_parents:
                raise _invalid("git_inventory_limit")
            parent_edges += len(refs.parents)
            if (
                refs.tree.object_type != "tree"
                or any(p.object_type != "commit" for p in refs.parents)
                or any(
                    p.object_format != node.material.object_format
                    for p in (refs.tree, *refs.parents)
                )
            ):
                raise _invalid("git_inventory_graph_mismatch")
    if total_bytes > limits.max_body_bytes:
        raise _invalid("git_inventory_limit")
    return catalog, roles, total_bytes, tree_edges, parent_edges


def _inventory_shape(
    value: GitObjectInventory | GitInventoryScope, checkpoint: Callable[[], None]
) -> None:
    """核对完整根并集、逐路径展开、精确角色和声明计数。"""
    if not value.objects:
        raise _invalid()
    limits = value.limits
    if len(value.objects) > limits.max_objects:
        raise _invalid("git_inventory_limit")
    catalog, roles, total_bytes, tree_edges, parent_edges = _inventory_catalog(
        value, limits, checkpoint
    )
    _root_shape(value, catalog)
    root_roles: tuple[tuple[GitInventoryRole, GitObjectRead | None], ...] = (
        ("base_commit", value.roots.base_commit),
        ("base_tree", value.roots.base_tree),
        ("target_tree", value.roots.target_tree),
        ("delivery_commit", value.roots.delivery_commit),
    )
    for role, request in root_roles:
        checkpoint()
        if request is not None:
            _member(request, catalog)
            roles[request.object_id].add(role)
    base = _walk(value.roots.base_tree, catalog, value.platform, limits, checkpoint)
    target = _walk(value.roots.target_tree, catalog, value.platform, limits, checkpoint)
    reachable = base[2] | target[2] | {value.roots.base_commit.object_id}
    if value.roots.delivery_commit is not None:
        reachable.add(value.roots.delivery_commit.object_id)
    if reachable != catalog.keys():
        raise _invalid("git_inventory_graph_mismatch")
    for node in value.objects:
        checkpoint()
        for entry in node.tree_entries:
            checkpoint()
            roles[entry.child.object_id].add("tree_member")
    for node in value.objects:
        checkpoint()
        if node.roles != tuple(role for role in _ROLES if role in roles[node.material.object_id]):
            raise _invalid("git_inventory_graph_mismatch")
    expected = GitInventoryMetrics(
        len(catalog), total_bytes, tree_edges, parent_edges, base[0], target[0], base[1], target[1]
    )
    if value.metrics != expected:
        raise _invalid("git_inventory_graph_mismatch")


def _snapshot_inventory_shape(value: object, checkpoint: Callable[[], None]) -> GitObjectInventory:
    """先严格深层重建，再校验声明图，供摘要及完整持久入口共用。"""
    snapshot = _snapshot_model(value, GitObjectInventory, checkpoint)
    _inventory_shape(snapshot, checkpoint)
    checkpoint()
    return snapshot


def snapshot_git_object_inventory(
    value: object, *, checkpoint: Callable[[], None]
) -> GitObjectInventory:
    """深层重建并校验声明图/规范摘要；不验实际正文、MAC 或权威归属。"""
    from harnessix.delivery.git_inventory_wire import _check_inventory_digests

    snapshot = _snapshot_inventory_shape(value, checkpoint)
    _check_inventory_digests(snapshot, checkpoint)
    checkpoint()
    return snapshot


def snapshot_git_inventory_scope(
    value: object, *, checkpoint: Callable[[], None]
) -> GitInventoryScope:
    """严格深层重建并校验完整声明图；不验 CAS、摘要、归属或批准。"""
    snapshot = _snapshot_model(value, GitInventoryScope, checkpoint)
    _inventory_shape(snapshot, checkpoint)
    checkpoint()
    return snapshot


def snapshot_git_inventory_prefix_projection(
    value: object, *, checkpoint: Callable[[], None]
) -> GitInventoryPrefixProjection:
    """只作普通投影的严格重建；不重算 Seal 链或认证尾锚。"""
    return _snapshot_model(value, GitInventoryPrefixProjection, checkpoint)
