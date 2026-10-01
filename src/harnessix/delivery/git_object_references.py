"""完整原始 Git 对象的直接引用解析；不解析 Pack、不读取历史、不签发业务证明。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal, cast

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead, GitObjectType

GitTreeMode = Literal["40000", "100644", "100755", "120000", "160000"]
_MODES: dict[bytes, GitObjectType] = {
    b"40000": "tree",
    b"100644": "blob",
    b"100755": "blob",
    b"120000": "blob",
    b"160000": "commit",
}
_HEADER_NAME = re.compile(rb"[^\x00-\x20\x7f]+\Z")


def _invalid(code: str = "git_object_references_invalid") -> KernelError:
    return KernelError(code, "Git对象直接引用不符合完整解析契约")


def _capacity(value: int) -> None:
    if type(value) is not int or value < 0:
        raise _invalid("git_object_references_limit_invalid")


def _material(material: GitObjectMaterial, kind: str) -> GitObjectMaterial:
    if type(material) is not GitObjectMaterial:
        raise _invalid()
    try:
        snapshot = GitObjectMaterial(
            material.object_type, material.object_id, material.object_format, material.body
        )
    except (AttributeError, KernelError):
        raise _invalid() from None
    if snapshot.object_type != kind:
        raise _invalid()
    return snapshot


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    """原始 basename、规范模式与精确子对象请求；名字不进入 repr。"""

    mode: GitTreeMode
    name: bytes = field(repr=False)
    child: GitObjectRead


@dataclass(frozen=True, slots=True)
class GitCommitReferences:
    """只记录原 tree 及有序 parent；不把 parent 自动声明为外部历史边界。"""

    tree: GitObjectRead
    parents: tuple[GitObjectRead, ...]


def parse_git_tree(
    material: GitObjectMaterial, *, max_entries: int, checkpoint: Callable[[], None]
) -> tuple[GitTreeEntry, ...]:
    """按原始二进制格式解析全部条目，拒绝截断、非规范模式、重复与错误排序。"""
    _capacity(max_entries)
    snapshot = _material(material, "tree")
    width = 20 if snapshot.object_format == "sha1" else 32
    body = snapshot.body
    position = 0
    names: set[bytes] = set()
    previous: bytes | None = None
    entries: list[GitTreeEntry] = []
    checkpoint()
    while position < len(body):
        checkpoint()
        if len(entries) >= max_entries:
            raise _invalid("git_object_references_limit")
        space = body.find(b" ", position, position + 8)
        end = body.find(b"\0", space + 1) if space >= 0 else -1
        if end < 0 or end + 1 + width > len(body):
            raise _invalid()
        raw_mode, name = body[position:space], body[space + 1 : end]
        if (
            raw_mode not in _MODES
            or not name
            or name in {b".", b".."}
            or name.lower() == b".git"
            or b"/" in name
            or name in names
        ):
            raise _invalid()
        # Git 以目录名末尾的斜线比较排序，不能按 basename 字符串排序。
        key = name + (b"/" if raw_mode == b"40000" else b"\0")
        if previous is not None and key <= previous:
            raise _invalid()
        oid = body[end + 1 : end + 1 + width].hex()
        if oid == "0" * (width * 2):
            raise _invalid()
        child = GitObjectRead(_MODES[raw_mode], oid, snapshot.object_format)
        entries.append(GitTreeEntry(cast(GitTreeMode, raw_mode.decode("ascii")), name, child))
        names.add(name)
        previous = key
        position = end + 1 + width
    checkpoint()
    return tuple(entries)


def _headers(body: bytes, checkpoint: Callable[[], None]) -> Iterator[tuple[bytes, bytes]]:
    """保留扩展头的原始续行，仅识别头边界；消息正文不参与引用解析。"""
    separator = body.find(b"\n\n")
    if separator < 0:
        if not body.endswith(b"\n"):
            raise _invalid()
        separator = len(body) - 1
    header = body[:separator]
    if not header or b"\0" in header:
        raise _invalid()
    previous: bytes | None = None
    position = 0
    while position < len(header):
        checkpoint()
        end = header.find(b"\n", position)
        if end < 0:
            end = len(header)
        line = header[position:end]
        position = end + 1
        if line.startswith(b" "):
            if previous is None or previous in {b"tree", b"parent", b"author", b"committer"}:
                raise _invalid()
            # 原材料持有完整续行，解析结果无需再次复制大块签名正文。
            continue
        name, space, value = line.partition(b" ")
        if not space or _HEADER_NAME.fullmatch(name) is None:
            raise _invalid()
        previous = name
        yield name, value


def _pointer(
    value: bytes, kind: Literal["tree", "commit"], material: GitObjectMaterial
) -> GitObjectRead:
    try:
        oid = value.decode("ascii")
        request = GitObjectRead(kind, oid, material.object_format)
    except (UnicodeError, KernelError):
        raise _invalid() from None
    if not any(character != "0" for character in oid):
        raise _invalid()
    return request


def parse_git_commit(
    material: GitObjectMaterial, *, max_parents: int, checkpoint: Callable[[], None]
) -> GitCommitReferences:
    """严格提取唯一首行 tree 和连续有序 parent，不重写作者、扩展头或消息。"""
    _capacity(max_parents)
    snapshot = _material(material, "commit")
    checkpoint()
    headers = _headers(snapshot.body, checkpoint)
    first = next(headers, None)
    if first is None or first[0] != b"tree":
        raise _invalid()
    tree = _pointer(first[1], "tree", snapshot)
    parents: list[GitObjectRead] = []
    current = next(headers, None)
    while current is not None and current[0] == b"parent":
        checkpoint()
        if len(parents) >= max_parents:
            raise _invalid("git_object_references_limit")
        parents.append(_pointer(current[1], "commit", snapshot))
        current = next(headers, None)
    committer = next(headers, None)
    if (
        current is None
        or current[0] != b"author"
        or not current[1]
        or committer is None
        or committer[0] != b"committer"
        or not committer[1]
    ):
        raise _invalid()
    for name, _ in headers:
        if name in {b"tree", b"parent", b"author", b"committer"}:
            raise _invalid()
    checkpoint()
    return GitCommitReferences(tree, tuple(parents))
