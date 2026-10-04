"""原 CAS 的完整父历史编码与严格 Reader；摘要校验不授予执行权限。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import WorkspaceContract, WorkspaceResourceObservation
from harnessix.workspace.parent_closure_contracts import (
    WorkspaceParentChunkReference,
    WorkspaceParentClosureManifest,
    WorkspaceParentClosureReference,
    WorkspaceParentObservation,
    WorkspaceParentObservationChunk,
)
from harnessix.workspace.parent_closure_paths import (
    decode_path_nodes,
    parent_paths,
    path_node_payloads,
)
from harnessix.workspace.parent_closure_wire import (
    MAX_CLOSURE_BLOB_BYTES,
    MAX_CLOSURE_TOTAL_BYTES,
    canonical_bytes,
    observations_digest,
)
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2, snapshot_requests


@dataclass(frozen=True, slots=True)
class EncodedParentClosure:
    reference: WorkspaceParentClosureReference
    blobs: tuple[tuple[str, bytes], ...]


def encode_parent_closure(
    scope: Mapping[str, object],
    parents: Sequence[WorkspaceResourceObservation],
    target_digest: str,
    *,
    checkpoint: Callable[[], None],
) -> EncodedParentClosure:
    """纯内存编码，先块后 Manifest；上限同时约束单 Blob 和整个闭包。"""
    chunks, blobs = _encode_chunks(parents, checkpoint)
    platform = scope["platform"]
    if platform != "posix" and platform != "windows":
        raise KernelError("workspace_platform_unsupported", "Workspace平台不受支持")
    nodes = path_node_payloads([(item.location, item.path) for item in parents], platform)
    manifest = WorkspaceParentClosureManifest.model_validate_json(
        canonical_bytes(
            {
                **scope,
                "spec_version": "harnessix.workspace-parent-closure/v1",
                "target_set_digest": target_digest,
                "nodes": nodes,
                "chunks": [item.model_dump(mode="json") for item in chunks],
                "observations_digest": observations_digest(parents, checkpoint),
            }
        ),
        strict=True,
    )
    body = canonical_bytes(manifest.model_dump(mode="json", warnings="error"))
    if (
        len(body) > MAX_CLOSURE_BLOB_BYTES
        or len(body) + sum(len(raw) for _, raw in blobs) > MAX_CLOSURE_TOTAL_BYTES
    ):
        raise KernelError("workspace_snapshot_limit", "Workspace父目录闭包超过原字节上限")
    reference = WorkspaceParentClosureReference(
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        parent_count=len(parents),
        target_set_digest=target_digest,
    )
    return EncodedParentClosure(reference, (*blobs, (reference.sha256, body)))


def _encode_chunks(
    parents: Sequence[WorkspaceResourceObservation],
    checkpoint: Callable[[], None],
) -> tuple[list[WorkspaceParentChunkReference], list[tuple[str, bytes]]]:
    entries: list[WorkspaceParentObservation] = []
    chunks: list[WorkspaceParentChunkReference] = []
    blobs: list[tuple[str, bytes]] = []
    start = 0
    size = len(_chunk_body(start, []))
    for parent in parents:
        if parent.path == "." and parent.kind != "directory":
            raise KernelError("workspace_closure_corrupt", "父目录历史根不是目录")
        checkpoint()
        entry = WorkspaceParentObservation.model_validate_json(
            canonical_bytes(parent.model_dump(mode="json", exclude={"location", "path", "access"})),
            strict=True,
        )
        increment = len(canonical_bytes(entry.model_dump(mode="json"))) + bool(entries)
        if entries and size + increment > MAX_CLOSURE_BLOB_BYTES:
            _append_chunk(start, entries, chunks, blobs)
            start += len(entries)
            entries = []
            size = len(_chunk_body(start, []))
            increment -= 1
        entries.append(entry)
        size += increment
    if entries:
        _append_chunk(start, entries, chunks, blobs)
    return chunks, blobs


def _chunk_body(start: int, entries: Sequence[WorkspaceParentObservation]) -> bytes:
    return canonical_bytes(
        {
            "spec_version": "harnessix.workspace-parent-observations/v1",
            "start_index": start,
            "entries": [item.model_dump(mode="json", warnings="error") for item in entries],
        }
    )


def _append_chunk(
    start: int,
    entries: Sequence[WorkspaceParentObservation],
    chunks: list[WorkspaceParentChunkReference],
    blobs: list[tuple[str, bytes]],
) -> None:
    body = _chunk_body(start, entries)
    digest = hashlib.sha256(body).hexdigest()
    chunks.append(
        WorkspaceParentChunkReference(
            sha256=digest,
            size=len(body),
            start_index=start,
            count=len(entries),
        )
    )
    blobs.append((digest, body))


def _read_model[T: WorkspaceContract](
    digest: str,
    size: int,
    model: type[T],
    read_blob: Callable[[str], bytes],
    checkpoint: Callable[[], None],
) -> T:
    body = read_verified_body(digest, size, read_blob, checkpoint)
    try:
        parsed = model.model_validate_json(body, strict=True)
        if canonical_bytes(parsed.model_dump(mode="json", warnings="error")) != body:
            raise ValueError
        return parsed
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise KernelError("workspace_closure_corrupt", "Workspace父目录历史损坏") from None


def read_verified_body(
    digest: str,
    size: int,
    read_blob: Callable[[str], bytes],
    checkpoint: Callable[[], None],
) -> bytes:
    """原 CAS 完整回读；上游控制异常不落入数据错误转换分支。"""
    checkpoint()
    try:
        body = read_blob(digest)
    except KernelError as error:
        if error.code in {"delivery_blob_corrupt", "delivery_blob_invalid"}:
            raise KernelError("workspace_closure_corrupt", "Workspace父目录历史损坏") from None
        raise
    checkpoint()
    try:
        if (
            type(body) is not bytes
            or len(body) != size
            or size > MAX_CLOSURE_BLOB_BYTES
            or hashlib.sha256(body).hexdigest() != digest
        ):
            raise ValueError
        return body
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise KernelError("workspace_closure_corrupt", "Workspace父目录历史损坏") from None


def snapshot_scope(snapshot: WorkspaceSnapshotV2) -> dict[str, object]:
    return snapshot.model_dump(
        mode="json",
        include={
            "platform",
            "workspace_id",
            "root_path_digest",
            "root_identity",
            "cwd",
            "external_roots",
        },
        warnings="error",
    )


def read_workspace_parent_closure(
    snapshot: WorkspaceSnapshotV2,
    read_blob: Callable[[str], bytes],
    *,
    checkpoint: Callable[[], None],
) -> tuple[WorkspaceResourceObservation, ...]:
    """所有历史解引用完成才能返回；任何坏块都不能返回已读前缀。"""
    checkpoint()
    try:
        snapshot = WorkspaceSnapshotV2.model_validate_json(
            snapshot.model_dump_json(warnings="error"), strict=True
        )
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise KernelError("workspace_closure_corrupt", "Workspace父目录引用合同无效") from None
    checkpoint()
    reference = snapshot.parent_closure
    manifest = _read_model(
        reference.sha256, reference.size, WorkspaceParentClosureManifest, read_blob, checkpoint
    )
    try:
        actual_scope = manifest.model_dump(mode="json", include=set(snapshot_scope(snapshot)))
        paths = decode_path_nodes(manifest.nodes, snapshot.platform)
        if (
            actual_scope != snapshot_scope(snapshot)
            or manifest.target_set_digest != reference.target_set_digest
            or paths != parent_paths(snapshot_requests(snapshot), snapshot.platform)
        ):
            raise ValueError
        if (
            len(paths) != reference.parent_count
            or reference.size + sum(item.size for item in manifest.chunks) > MAX_CLOSURE_TOTAL_BYTES
        ):
            raise ValueError
    except (ValueError, TypeError, KernelError):
        raise KernelError("workspace_closure_corrupt", "Workspace父目录历史绑定无效") from None
    parents: list[WorkspaceResourceObservation] = []
    for chunk_ref in manifest.chunks:
        chunk = _read_model(
            chunk_ref.sha256, chunk_ref.size, WorkspaceParentObservationChunk, read_blob, checkpoint
        )
        if chunk.start_index != chunk_ref.start_index or len(chunk.entries) != chunk_ref.count:
            raise KernelError("workspace_closure_corrupt", "Workspace父目录历史索引无效")
        for index, entry in enumerate(chunk.entries, start=chunk.start_index):
            checkpoint()
            location, path = paths[index]
            parents.append(
                WorkspaceResourceObservation(
                    location=location,
                    path=path,
                    access="read",
                    **entry.model_dump(),
                )
            )
    if observations_digest(parents, checkpoint) != manifest.observations_digest:
        raise KernelError("workspace_closure_corrupt", "Workspace父目录完整观察摘要无效")
    _validate_shared_observations(snapshot, parents)
    return tuple(parents)


def _validate_shared_observations(
    snapshot: WorkspaceSnapshotV2,
    parents: Sequence[WorkspaceResourceObservation],
) -> None:
    from harnessix.workspace.paths import path_comparison_key

    explicit = {
        (item.location, path_comparison_key(item.path, snapshot.platform)): item
        for item in snapshot.resources
        if item.access == "read"
    }
    for parent in parents:
        shared = explicit.get(
            (parent.location, path_comparison_key(parent.path, snapshot.platform))
        )
        if parent.path == "." and parent.kind != "directory":
            raise KernelError("workspace_closure_corrupt", "父目录历史根不是目录")
        if shared is not None and shared.model_dump(exclude={"path"}) != parent.model_dump(
            exclude={"path"}
        ):
            raise KernelError("workspace_closure_corrupt", "显式资源与父目录历史不一致")
