"""确定性派生全部严格祖先，并以单组件字典保留完整逻辑路径。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harnessix.workspace.contracts import PlatformKind, WorkspaceResourceRequest
from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key

MAX_PARENT_PATHS = 256 * 128


class PathNodeFields(Protocol):
    parent: int | None
    name: str
    location: str | None


def parent_paths(
    requests: Sequence[WorkspaceResourceRequest],
    platform: PlatformKind,
) -> tuple[tuple[str, str], ...]:
    """共享祖先按平台语义去重；等价 Windows 前缀选最小原拼写。"""
    components: dict[tuple[str, str], str] = {}
    for request in requests:
        path = normalize_workspace_path(request.path, platform)
        parts = [] if path == "." else path.split("/")
        ancestors = [".", *("/".join(parts[:i]) for i in range(1, len(parts)))]
        for ancestor in ancestors:
            parent_key = (request.location, path_comparison_key(ancestor, platform))
            name = ancestor.rsplit("/", 1)[-1]
            components[parent_key] = min(
                components.get(parent_key, name),
                name,
                key=lambda value: (len(value.encode()), value),
            )
    paths: dict[tuple[str, str], str] = {}
    for location, key in sorted(components, key=lambda item: (item[0], item[1] != ".", item[1])):
        prefix, _, _ = key.rpartition("/")
        paths[location, key] = (
            components[location, key]
            if key == "." or not prefix
            else f"{paths[location, prefix]}/{components[location, key]}"
        )
    return tuple((location, path) for (location, _), path in paths.items())


def target_set_payload(
    requests: Sequence[WorkspaceResourceRequest],
    platform: PlatformKind,
) -> list[dict[str, str]]:
    return [
        item.model_dump(mode="json", warnings="error")
        for item in sorted(
            requests,
            key=lambda item: (item.location, path_comparison_key(item.path, platform), item.access),
        )
    ]


def path_node_payloads(
    paths: Sequence[tuple[str, str]],
    platform: PlatformKind,
) -> list[dict[str, object]]:
    """字典索引只指向已出现父项，叶路径仍由原请求持有。"""
    indices: dict[tuple[str, str], int] = {}
    nodes: list[dict[str, object]] = []
    for location, path in paths:
        node: dict[str, object]
        if path == ".":
            node = {"parent": None, "name": ".", "location": location}
        else:
            prefix, _, name = path.rpartition("/")
            parent_key = (location, path_comparison_key(prefix or ".", platform))
            node = {"parent": indices[parent_key], "name": name, "location": None}
        indices[location, path_comparison_key(path, platform)] = len(nodes)
        nodes.append(node)
    return nodes


def decode_path_nodes(
    nodes: Sequence[PathNodeFields],
    platform: PlatformKind,
) -> tuple[tuple[str, str], ...]:
    """原字典组件重建与平台规范校验；拒绝回边、逃逸和等价重复。"""
    paths: list[tuple[str, str]] = []
    for index, raw in enumerate(nodes):
        # 模型在调用前验证类型；此处只依赖节点的正式字段。
        parent, name, location = raw.parent, raw.name, raw.location
        if parent is None:
            if name != "." or location is None:
                raise ValueError("父路径字典根节点无效")
            path = "."
        else:
            if parent >= index or location is not None or name in {".", ".."} or "/" in name:
                raise ValueError("父路径字典索引或组件无效")
            location, prefix = paths[parent]
            path = name if prefix == "." else f"{prefix}/{name}"
        if normalize_workspace_path(path, platform) != path:
            raise ValueError("父路径字典路径不规范")
        paths.append((location, path))
    keys = [(location, path_comparison_key(path, platform)) for location, path in paths]
    if keys != sorted(set(keys), key=lambda item: (item[0], item[1] != ".", item[1])):
        raise ValueError("父路径字典重复或顺序无效")
    return tuple(paths)
