"""版本化父目录完整历史：引用、路径字典及有界观察分块合同。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import ExternalRoot, PlatformKind, WorkspaceContract
from harnessix.workspace.parent_closure_paths import MAX_PARENT_PATHS, decode_path_nodes
from harnessix.workspace.parent_closure_wire import MAX_CLOSURE_BLOB_BYTES


class WorkspaceParentClosureReference(WorkspaceContract):
    sha256: Revision
    size: int = Field(ge=1, le=MAX_CLOSURE_BLOB_BYTES)
    parent_count: int = Field(ge=1, le=MAX_PARENT_PATHS)
    target_set_digest: Revision


class WorkspaceParentPathNode(WorkspaceContract):
    parent: int | None = Field(ge=0, lt=MAX_PARENT_PATHS)
    name: str = Field(min_length=1, max_length=4096)
    location: str | None = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")


class WorkspaceParentObservation(WorkspaceContract):
    """索引隐含路径和 read；完整目录身份及直接成员摘要由原 identity 绑定。"""

    kind: Literal["directory", "missing"]
    identity: Revision
    content_sha256: None = None
    size: int = Field(ge=0)

    @model_validator(mode="after")
    def missing_is_empty(self) -> Self:
        if self.kind == "missing" and self.size != 0:
            raise ValueError("缺失父目录大小必须为零")
        return self


class WorkspaceParentObservationChunk(WorkspaceContract):
    spec_version: Literal["harnessix.workspace-parent-observations/v1"] = (
        "harnessix.workspace-parent-observations/v1"
    )
    start_index: int = Field(ge=0, lt=MAX_PARENT_PATHS)
    entries: tuple[WorkspaceParentObservation, ...] = Field(
        min_length=1, max_length=MAX_PARENT_PATHS
    )


class WorkspaceParentChunkReference(WorkspaceContract):
    sha256: Revision
    size: int = Field(ge=1, le=MAX_CLOSURE_BLOB_BYTES)
    start_index: int = Field(ge=0, lt=MAX_PARENT_PATHS)
    count: int = Field(ge=1, le=MAX_PARENT_PATHS)


class WorkspaceParentClosureManifest(WorkspaceContract):
    spec_version: Literal["harnessix.workspace-parent-closure/v1"] = (
        "harnessix.workspace-parent-closure/v1"
    )
    platform: PlatformKind
    workspace_id: Revision
    root_path_digest: Revision
    root_identity: Revision
    cwd: str = Field(min_length=1, max_length=4096)
    external_roots: tuple[ExternalRoot, ...] = Field(default=(), max_length=16)
    target_set_digest: Revision
    nodes: tuple[WorkspaceParentPathNode, ...] = Field(min_length=1, max_length=MAX_PARENT_PATHS)
    chunks: tuple[WorkspaceParentChunkReference, ...] = Field(
        min_length=1, max_length=MAX_PARENT_PATHS
    )
    observations_digest: Revision

    @model_validator(mode="after")
    def complete_dictionary_and_chunks(self) -> Self:
        try:
            decode_path_nodes(self.nodes, self.platform)
        except KernelError:
            raise ValueError("父路径字典不满足平台规范") from None
        cursor = 0
        digests: set[str] = set()
        for chunk in self.chunks:
            if chunk.start_index != cursor or chunk.sha256 in digests:
                raise ValueError("父目录分块重复或顺序不连续")
            cursor += chunk.count
            digests.add(chunk.sha256)
        if cursor != len(self.nodes):
            raise ValueError("父目录分块未完整覆盖字典")
        return self
