"""原 CAS 上完整普通文件树的只读验真；不补对象、不写库、不授予交付执行权。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    pure_git_authentication,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectRead
from harnessix.delivery.git_object_references import GitTreeEntry, parse_git_tree
from harnessix.workspace.contracts import PlatformKind
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key


def _invalid(code: str = "git_tree_closure_invalid") -> KernelError:
    return KernelError(code, "Git完整文件树材料验真失败")


@dataclass(frozen=True, slots=True)
class GitTreeClosureLimits:
    """由受信宿主明确给出的内部验真限额；无产品默认值或额度豁免。"""

    max_objects: int
    max_body_bytes: int
    max_entries: int
    max_depth: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (self.max_objects, self.max_body_bytes, self.max_entries, self.max_depth)
        ):
            raise _invalid("git_tree_closure_limit_invalid")


@dataclass(frozen=True, slots=True)
class GitTreeFile:
    """完整树中的普通文件；模式变化独立于正文去重，路径不进入 repr。"""

    path: str = field(repr=False)
    mode: Literal["100644", "100755"]
    material: GitObjectMaterialReference


@dataclass(frozen=True, slots=True)
class GitTreeClosure:
    """仅表示本次完整内容观察；不是认证业务目录、共同静默快照或批准证明。"""

    root: GitObjectMaterialReference
    objects: tuple[GitObjectMaterialReference, ...]
    files: tuple[GitTreeFile, ...] = field(repr=False)
    body_bytes: int
    expanded_entries: int
    tree_depth: int


def _reference(value: GitObjectMaterialReference) -> GitObjectMaterialReference:
    if type(value) is not GitObjectMaterialReference:
        raise _invalid()
    try:
        return GitObjectMaterialReference.from_binding(value.binding())
    except (AttributeError, KernelError):
        raise _invalid() from None


def _catalog(
    root: GitObjectMaterialReference,
    catalog: tuple[GitObjectMaterialReference, ...],
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> dict[str, GitObjectMaterialReference]:
    if type(checkpoint) is GitAuthenticationControl:
        with pure_git_authentication(checkpoint) as check:
            return _catalog(root, catalog, limits, check)
    if type(catalog) is not tuple:
        raise _invalid()
    if len(catalog) > limits.max_objects:
        raise _invalid("git_tree_closure_limit")
    result: dict[str, GitObjectMaterialReference] = {}
    for value in catalog:
        checkpoint()
        reference = _reference(value)
        if reference.object_format != root.object_format or reference.object_id in result:
            raise _invalid()
        result[reference.object_id] = reference
    if root.object_id in result and result[root.object_id] != root:
        raise _invalid()
    result[root.object_id] = root
    if len(result) > limits.max_objects:
        raise _invalid("git_tree_closure_limit")
    return result


@dataclass
class _Traversal:
    """本次只读观察的唯一计数器；对象去重不省略重复子树的路径展开。"""

    cas: GitMaterialCAS = field(repr=False)
    catalog: dict[str, GitObjectMaterialReference] = field(repr=False)
    limits: GitTreeClosureLimits
    checkpoint: Callable[[], None] = field(repr=False)
    observed: dict[str, GitObjectMaterialReference] = field(default_factory=dict, repr=False)
    trees: dict[str, tuple[GitTreeEntry, ...]] = field(default_factory=dict, repr=False)
    paths: set[str] = field(default_factory=set, repr=False)
    body_bytes: int = 0
    expanded_entries: int = 0
    tree_depth: int = 0

    def read(self, request: GitObjectRead) -> GitObjectMaterialReference:
        self.checkpoint()
        reference = self.catalog.get(request.object_id)
        if reference is None:
            raise _invalid("git_tree_closure_missing")
        if reference.object_type != request.object_type:
            raise _invalid("git_tree_closure_type_mismatch")
        if request.object_id in self.observed:
            return reference
        if (
            len(self.observed) >= self.limits.max_objects
            or self.body_bytes + reference.body_bytes > self.limits.max_body_bytes
        ):
            raise _invalid("git_tree_closure_limit")
        material = self.cas.read(reference)
        # 入口完整认证对应原回读后检查；CAS 本身始终在纯段之外。
        with pure_git_authentication(self.checkpoint) as check:
            check()
            if material.object_type == "tree":
                self.trees[request.object_id] = parse_git_tree(
                    material,
                    max_entries=self.limits.max_entries - self.expanded_entries,
                    checkpoint=check,
                )
        self.observed[request.object_id] = reference
        self.body_bytes += reference.body_bytes
        return reference

    def path(self, prefix: str, name: bytes, platform: PlatformKind) -> str:
        try:
            basename = name.decode("utf-8", errors="strict")
            path = prefix + "/" + basename if prefix else basename
            if normalize_workspace_path(path, platform) != path:
                raise ValueError
            key = path_comparison_key(path, platform)
        except (UnicodeError, KernelError, ValueError):
            raise _invalid("git_tree_closure_path_denied") from None
        if key in self.paths:
            raise _invalid("git_tree_closure_path_denied")
        self.paths.add(key)
        return path


def verify_git_tree_closure(
    cas: GitMaterialCAS,
    root: GitObjectMaterialReference,
    catalog: tuple[GitObjectMaterialReference, ...],
    *,
    platform: PlatformKind,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> GitTreeClosure:
    """所有目录和普通文件闭合后才返回；缺失、超限或取消不返回部分文件树。"""
    if type(cas) is not GitMaterialCAS or type(limits) is not GitTreeClosureLimits:
        raise _invalid()
    if type(platform) is not str or platform not in {"posix", "windows"}:
        raise _invalid("git_tree_closure_path_denied")
    try:
        limits = GitTreeClosureLimits(
            limits.max_objects, limits.max_body_bytes, limits.max_entries, limits.max_depth
        )
    except (AttributeError, KernelError):
        raise _invalid("git_tree_closure_limit_invalid") from None
    root = _reference(root)
    if root.object_type != "tree":
        raise _invalid("git_tree_closure_type_mismatch")
    state = _Traversal(cas, _catalog(root, catalog, limits, checkpoint), limits, checkpoint)
    files: list[GitTreeFile] = []
    # 迭代 DFS 避免 Python 递归限制；同一 tree 可在多个路径合法复用。
    pending = [(GitObjectRead("tree", root.object_id, root.object_format), "", 0, ())]
    while pending:
        state.checkpoint()
        request, prefix, depth, ancestors = pending.pop()
        if depth > limits.max_depth:
            raise _invalid("git_tree_closure_limit")
        if request.object_id in ancestors:
            raise _invalid("git_tree_closure_cycle")
        state.read(request)
        entries = state.trees[request.object_id]
        if state.expanded_entries + len(entries) > limits.max_entries:
            raise _invalid("git_tree_closure_limit")
        state.expanded_entries += len(entries)
        state.tree_depth = max(state.tree_depth, depth)
        children = []
        for entry in entries:
            state.checkpoint()
            path = state.path(prefix, entry.name, platform)
            if entry.mode == "40000":
                children.append((entry.child, path, depth + 1, (*ancestors, request.object_id)))
            elif entry.mode in {"100644", "100755"}:
                reference = state.read(entry.child)
                files.append(
                    GitTreeFile(path, cast(Literal["100644", "100755"], entry.mode), reference)
                )
            else:
                raise _invalid("git_tree_closure_unsupported")
        pending.extend(reversed(children))
    state.checkpoint()
    return GitTreeClosure(
        root,
        tuple(state.observed[oid] for oid in sorted(state.observed)),
        tuple(sorted(files, key=lambda item: item.path.encode("utf-8"))),
        state.body_bytes,
        state.expanded_entries,
        state.tree_depth,
    )
