"""完整目标普通文件树的纯投影；不写 CAS、Workspace、Git 对象库或 Ref。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import (
    MAX_TRANSACTION_FILE_BYTES,
    MAX_TRANSACTION_FILES,
    MAX_TRANSACTION_IMAGE_BYTES,
    PROTECTED_COMPONENTS,
    WorkspaceFileVersion,
    WorkspaceMutation,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS, GitObjectMaterialReference
from harnessix.delivery.git_object_material import GitObjectMaterial
from harnessix.delivery.git_object_references import parse_git_tree
from harnessix.delivery.git_tree_closure import (
    GitTreeClosure,
    GitTreeClosureLimits,
    GitTreeFile,
    verify_git_tree_closure,
)
from harnessix.workspace.contracts import PlatformKind
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key


def _invalid(code: str = "git_tree_projection_invalid") -> KernelError:
    return KernelError(code, "Git完整目标文件树规划失败")


@dataclass(frozen=True, slots=True)
class GitTreeProjection:
    """仅包含完整目标内容与容量事实，不表示业务认证、执行批准或产品发布。"""

    base: GitTreeClosure = field(repr=False)
    root: GitObjectMaterial = field(repr=False)
    new_trees: tuple[GitObjectMaterial, ...] = field(repr=False)
    files: tuple[GitTreeFile, ...] = field(repr=False)
    objects_body_bytes: int
    expanded_entries: int
    tree_depth: int


def _version(value: WorkspaceFileVersion) -> WorkspaceFileVersion:
    if type(value) is not WorkspaceFileVersion:
        raise _invalid()
    try:
        if set(vars(value)) != {"presence", "sha256", "size", "mode"}:
            raise _invalid()
        if (
            value.__pydantic_extra__ is not None
            or type(value.presence) is not str
            or type(value.size) is not int
            or (value.sha256 is not None and type(value.sha256) is not str)
            or (value.mode is not None and type(value.mode) is not int)
        ):
            raise _invalid()
        return WorkspaceFileVersion(**vars(value))
    except (AttributeError, ValidationError):
        raise _invalid() from None


def _mutation(value: WorkspaceMutation) -> WorkspaceMutation:
    if type(value) is not WorkspaceMutation:
        raise _invalid()
    try:
        if (
            set(vars(value)) != {"path", "before", "after"}
            or value.__pydantic_extra__ is not None
            or type(value.path) is not str
        ):
            raise _invalid()
        return WorkspaceMutation(
            path=value.path, before=_version(value.before), after=_version(value.after)
        )
    except (AttributeError, ValidationError):
        raise _invalid() from None


def _mutations(
    values: tuple[WorkspaceMutation, ...], platform: PlatformKind, checkpoint: Callable[[], None]
) -> tuple[WorkspaceMutation, ...]:
    if type(values) is not tuple or not 1 <= len(values) <= MAX_TRANSACTION_FILES:
        raise _invalid()
    result = []
    keys = []
    for value in values:
        checkpoint()
        item = _mutation(value)
        try:
            if normalize_workspace_path(item.path, platform) != item.path or item.path == ".":
                raise _invalid("git_tree_projection_path_denied")
            key = path_comparison_key(item.path, platform)
        except KernelError:
            raise _invalid("git_tree_projection_path_denied") from None
        if any(
            part.casefold() in PROTECTED_COMPONENTS or part.casefold().startswith(".env")
            for part in item.path.split("/")
        ):
            raise _invalid("git_tree_projection_path_denied")
        result.append(item)
        keys.append(key)
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise _invalid("git_tree_projection_path_denied")
    if sum(item.before.size + item.after.size for item in result) > MAX_TRANSACTION_IMAGE_BYTES:
        raise _invalid("git_tree_projection_limit")
    return tuple(result)


def _reference(value: GitObjectMaterialReference) -> GitObjectMaterialReference:
    if type(value) is not GitObjectMaterialReference:
        raise _invalid()
    try:
        return GitObjectMaterialReference.from_binding(value.binding())
    except (AttributeError, KernelError):
        raise _invalid() from None


def _material_reference(material: GitObjectMaterial) -> GitObjectMaterialReference:
    return GitObjectMaterialReference(
        material.object_type,
        material.object_id,
        material.object_format,
        material.body_sha256,
        material.body_bytes,
        material.body_sha256,
    )


@dataclass(slots=True)
class _Objects:
    """预算覆盖 base、显式 after 与新目录对象的唯一 OID 并集。"""

    limits: GitTreeClosureLimits
    references: dict[str, GitObjectMaterialReference] = field(default_factory=dict, repr=False)
    body_bytes: int = 0

    def add(self, reference: GitObjectMaterialReference) -> None:
        previous = self.references.get(reference.object_id)
        if previous is not None:
            if previous != reference:
                raise _invalid()
            return
        if (
            len(self.references) >= self.limits.max_objects
            or self.body_bytes + reference.body_bytes > self.limits.max_body_bytes
        ):
            raise _invalid("git_tree_projection_limit")
        self.references[reference.object_id] = reference
        self.body_bytes += reference.body_bytes


def _directories(
    cas: GitMaterialCAS,
    base: GitTreeClosure,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> set[str]:
    trees = {}
    for reference in base.objects:
        checkpoint()
        if reference.object_type == "tree":
            trees[reference.object_id] = parse_git_tree(
                cas.read(reference), max_entries=limits.max_entries, checkpoint=checkpoint
            )
    result = {""}
    pending = [(base.root.object_id, "")]
    while pending:
        checkpoint()
        oid, prefix = pending.pop()
        for entry in trees[oid]:
            checkpoint()
            if entry.mode == "40000":
                name = entry.name.decode("utf-8")
                path = prefix + "/" + name if prefix else name
                result.add(path)
                pending.append((entry.child.object_id, path))
    return result


def _before(
    base: GitTreeClosure,
    directories: set[str],
    mutations: tuple[WorkspaceMutation, ...],
    platform: PlatformKind,
    checkpoint: Callable[[], None],
) -> dict[str, GitTreeFile]:
    files = {item.path: item for item in base.files}
    directory_keys = {path_comparison_key(path, platform) for path in directories if path}
    for mutation in mutations:
        checkpoint()
        current = files.get(mutation.path)
        expected = WorkspaceFileVersion(presence="absent", size=0)
        if current is not None:
            expected = WorkspaceFileVersion(
                presence="file",
                sha256=current.material.body_sha256,
                size=current.material.body_bytes,
                mode=420 if current.mode == "100644" else 493,
            )
        if (
            mutation.before != expected
            or path_comparison_key(mutation.path, platform) in directory_keys
        ):
            raise _invalid("git_tree_projection_before_mismatch")
    return files


def _after(
    cas: GitMaterialCAS,
    references: tuple[GitObjectMaterialReference, ...],
    mutations: tuple[WorkspaceMutation, ...],
    object_format: str,
    objects: _Objects,
    checkpoint: Callable[[], None],
) -> dict[tuple[str, int], GitObjectMaterialReference]:
    if type(references) is not tuple or len(references) > MAX_TRANSACTION_FILES:
        raise _invalid()
    required = {
        (item.after.sha256, item.after.size) for item in mutations if item.after.presence == "file"
    }
    result: dict[tuple[str, int], GitObjectMaterialReference] = {}
    for value in references:
        checkpoint()
        reference = _reference(value)
        key = (reference.body_sha256, reference.body_bytes)
        if (
            reference.object_type != "blob"
            or reference.object_format != object_format
            or key not in required
            or key in result
        ):
            raise _invalid("git_tree_projection_after_mismatch")
        objects.add(reference)
        cas.read(reference)
        checkpoint()
        result[key] = reference
    if set(result) != required:
        raise _invalid("git_tree_projection_after_mismatch")
    return result


def _parents(path: str) -> tuple[str, ...]:
    parts = path.split("/")
    return tuple("/".join(parts[:index]) for index in range(1, len(parts)))


def _apply(
    files: dict[str, GitTreeFile],
    directories: set[str],
    mutations: tuple[WorkspaceMutation, ...],
    after: dict[tuple[str, int], GitObjectMaterialReference],
    checkpoint: Callable[[], None],
) -> None:
    touched: set[str] = set()
    # 先完成所有删除，再加入目标文件；同一对象的两个目录仍按路径独立修改。
    for item in mutations:
        checkpoint()
        if item.before.presence == "file":
            del files[item.path]
        if item.after.presence == "absent":
            touched.update(_parents(item.path))
    for item in mutations:
        checkpoint()
        if item.after.presence == "file":
            reference = after[(cast(str, item.after.sha256), item.after.size)]
            mode: Literal["100644", "100755"] = "100644" if item.after.mode == 420 else "100755"
            files[item.path] = GitTreeFile(item.path, mode, reference)
            directories.update(_parents(item.path))
    children: dict[str, int] = dict.fromkeys(directories, 0)
    for path in (*files, *(path for path in directories if path)):
        parent = path.rpartition("/")[0]
        children[parent] += 1
    for path in sorted(touched, key=lambda value: value.count("/"), reverse=True):
        checkpoint()
        if path in directories and children[path] == 0:
            directories.remove(path)
            children[path.rpartition("/")[0]] -= 1


def _namespace(
    files: dict[str, GitTreeFile],
    directories: set[str],
    platform: PlatformKind,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> tuple[int, int]:
    keys = set()
    entries = len(files) + len(directories) - 1
    depth = max((path.count("/") + 1 for path in directories if path), default=0)
    if entries > limits.max_entries or depth > limits.max_depth:
        raise _invalid("git_tree_projection_limit")
    for path in (*files, *(path for path in directories if path)):
        checkpoint()
        try:
            if normalize_workspace_path(path, platform) != path:
                raise ValueError
            key = path_comparison_key(path, platform)
        except (KernelError, ValueError):
            raise _invalid("git_tree_projection_path_denied") from None
        if key in keys:
            raise _invalid("git_tree_projection_path_denied")
        keys.add(key)
    return entries, depth


def _encode(
    rows: list[tuple[str, bytes, str]],
    object_format: Literal["sha1", "sha256"],
    checkpoint: Callable[[], None],
) -> GitObjectMaterial:
    width = 20 if object_format == "sha1" else 32
    size = sum(len(mode) + 2 + len(name) + width for mode, name, _ in rows)
    if size > MAX_TRANSACTION_FILE_BYTES:
        raise _invalid("git_tree_projection_limit")
    rows.sort(key=lambda row: row[1] + (b"/" if row[0] == "40000" else b"\0"))
    parts = []
    for mode, name, oid in rows:
        checkpoint()
        parts.append(mode.encode("ascii") + b" " + name + b"\0" + bytes.fromhex(oid))
    return GitObjectMaterial.from_body("tree", object_format, b"".join(parts))


def _trees(
    files: dict[str, GitTreeFile],
    directories: set[str],
    objects: _Objects,
    object_format: Literal["sha1", "sha256"],
    checkpoint: Callable[[], None],
) -> tuple[GitObjectMaterial, tuple[GitObjectMaterial, ...]]:
    children: dict[str, list[str]] = {path: [] for path in directories}
    rows: dict[str, list[tuple[str, bytes, str]]] = {path: [] for path in directories}
    for path, item in files.items():
        checkpoint()
        parent, _, name = path.rpartition("/")
        rows[parent].append((item.mode, name.encode("utf-8"), item.material.object_id))
    for path in directories:
        if path:
            children[path.rpartition("/")[0]].append(path)
    encoded: dict[str, GitObjectMaterial] = {}
    new = {}
    original_oids = set(objects.references)
    for path in sorted(directories, key=lambda value: value.count("/") + bool(value), reverse=True):
        checkpoint()
        for child in children[path]:
            rows[path].append(
                ("40000", child.rpartition("/")[2].encode("utf-8"), encoded[child].object_id)
            )
        material = _encode(rows[path], object_format, checkpoint)
        objects.add(_material_reference(material))
        encoded[path] = material
        if material.object_id not in original_oids:
            new[material.object_id] = material
    return encoded[""], tuple(new[oid] for oid in sorted(new))


def prepare_git_tree_projection(
    cas: GitMaterialCAS,
    root: GitObjectMaterialReference,
    catalog: tuple[GitObjectMaterialReference, ...],
    mutations: tuple[WorkspaceMutation, ...],
    after_catalog: tuple[GitObjectMaterialReference, ...],
    *,
    platform: PlatformKind,
    limits: GitTreeClosureLimits,
    checkpoint: Callable[[], None],
) -> GitTreeProjection:
    """完整验 base 和变化镜像，纯计算完整目标；任何失败或取消均无部分结果。"""
    if (
        type(cas) is not GitMaterialCAS
        or type(limits) is not GitTreeClosureLimits
        or not callable(checkpoint)
    ):
        raise _invalid()
    if type(platform) is not str or platform not in {"posix", "windows"}:
        raise _invalid("git_tree_projection_path_denied")
    try:
        limits = GitTreeClosureLimits(
            limits.max_objects, limits.max_body_bytes, limits.max_entries, limits.max_depth
        )
    except (AttributeError, KernelError):
        raise _invalid() from None
    mutations = _mutations(mutations, platform, checkpoint)
    base = verify_git_tree_closure(
        cas, root, catalog, platform=platform, limits=limits, checkpoint=checkpoint
    )
    directories = _directories(cas, base, limits, checkpoint)
    files = _before(base, directories, mutations, platform, checkpoint)
    objects = _Objects(limits)
    for reference in base.objects:
        checkpoint()
        objects.add(reference)
    after = _after(cas, after_catalog, mutations, base.root.object_format, objects, checkpoint)
    _apply(files, directories, mutations, after, checkpoint)
    entries, depth = _namespace(files, directories, platform, limits, checkpoint)
    target, new = _trees(files, directories, objects, base.root.object_format, checkpoint)
    checkpoint()
    return GitTreeProjection(
        base,
        target,
        new,
        tuple(sorted(files.values(), key=lambda item: item.path.encode("utf-8"))),
        objects.body_bytes,
        entries,
        depth,
    )
