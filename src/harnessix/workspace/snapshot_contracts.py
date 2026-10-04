"""Snapshot v2 将显式资源与完整派生父历史分别承载，不扩张旧 v1。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import (
    ExternalRoot,
    PlatformKind,
    WorkspaceContract,
    WorkspaceResourceObservation,
    WorkspaceResourceRequest,
)
from harnessix.workspace.parent_closure_contracts import WorkspaceParentClosureReference
from harnessix.workspace.parent_closure_paths import parent_paths, target_set_payload
from harnessix.workspace.parent_closure_wire import canonical_digest
from harnessix.workspace.snapshot_fields import validate_snapshot_resources


class WorkspaceSnapshotV2(WorkspaceContract):
    spec_version: Literal["harnessix.workspace-snapshot/v2"] = "harnessix.workspace-snapshot/v2"
    platform: PlatformKind
    workspace_id: Revision
    root_path_digest: Revision
    root_identity: Revision
    cwd: str = Field(min_length=1, max_length=4096)
    external_roots: tuple[ExternalRoot, ...] = Field(default=(), max_length=16)
    resources: tuple[WorkspaceResourceObservation, ...] = Field(default=(), max_length=256)
    parent_closure: WorkspaceParentClosureReference
    algorithm: Literal["selected-resources-parent-closure-sha256/v2"] = (
        "selected-resources-parent-closure-sha256/v2"
    )
    revision: Revision

    @model_validator(mode="after")
    def complete_resource_binding(self) -> Self:
        validate_snapshot_resources(self)
        used = {item.location for item in self.resources}
        if any(root.location in used and "read" not in root.access for root in self.external_roots):
            raise ValueError("派生父目录未获外部根read授权")
        identity = canonical_digest(
            {
                "platform": self.platform,
                "root_path_digest": self.root_path_digest,
                "root_identity": self.root_identity,
            }
        )
        requests = snapshot_requests(self)
        if self.workspace_id != identity:
            raise ValueError("Workspace根身份摘要不一致")
        if self.parent_closure.target_set_digest != canonical_digest(
            target_set_payload(requests, self.platform),
        ) or self.parent_closure.parent_count != len(parent_paths(requests, self.platform)):
            raise ValueError("父目录引用未绑定完整目标集合")
        if self.revision != canonical_digest(
            self.model_dump(
                mode="json",
                exclude={"spec_version", "revision"},
                warnings="error",
            )
        ):
            raise ValueError("Workspace Snapshot revision不一致")
        return self


def snapshot_requests(snapshot: WorkspaceSnapshotV2) -> tuple[WorkspaceResourceRequest, ...]:
    return tuple(
        WorkspaceResourceRequest(location=item.location, path=item.path, access=item.access)
        for item in snapshot.resources
    )
