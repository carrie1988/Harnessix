"""Workspace身份与租约：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from harnessix.tools.contracts import Revision

PlatformKind = Literal["posix", "windows"]
ResourceAccess = Literal["read", "write", "execute"]
ResourceKind = Literal["file", "directory", "missing"]


class WorkspaceContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class WorkspaceResourceRequest(WorkspaceContract):
    location: str = Field(default="workspace", pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    path: str = Field(min_length=1, max_length=4096)
    access: ResourceAccess


class ExternalRoot(WorkspaceContract):
    location: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    path_digest: Revision
    identity: Revision
    access: tuple[ResourceAccess, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_access(self) -> Self:
        order = {"read": 0, "write": 1, "execute": 2}
        if (
            self.location == "workspace"
            or tuple(dict.fromkeys(self.access)) != self.access
            or tuple(sorted(self.access, key=order.__getitem__)) != self.access
        ):
            raise ValueError("外部根标识或访问模式无效")
        return self


class WorkspaceResourceObservation(WorkspaceContract):
    location: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    path: str = Field(min_length=1, max_length=4096)
    access: ResourceAccess
    kind: ResourceKind
    identity: Revision
    content_sha256: Revision | None = None
    size: int = Field(ge=0)

    @model_validator(mode="after")
    def content_matches_kind(self) -> Self:
        if (self.content_sha256 is not None) != (self.kind == "file"):
            raise ValueError("只有文件观察可以携带内容摘要")
        if self.kind == "missing" and self.size != 0:
            raise ValueError("缺失资源大小必须为零")
        return self


class WorkspaceSnapshot(WorkspaceContract):
    spec_version: Literal["harnessix.workspace-snapshot/v1"] = "harnessix.workspace-snapshot/v1"
    platform: PlatformKind
    workspace_id: Revision
    root_path_digest: Revision
    root_identity: Revision
    cwd: str = Field(min_length=1, max_length=4096)
    external_roots: tuple[ExternalRoot, ...] = Field(default=(), max_length=16)
    resources: tuple[WorkspaceResourceObservation, ...] = Field(default=(), max_length=256)
    algorithm: Literal["selected-resources-sha256/v1"] = "selected-resources-sha256/v1"
    revision: Revision

    @model_validator(mode="after")
    def unique_resources(self) -> Self:
        from harnessix.workspace.paths import normalize_workspace_path, path_comparison_key

        roots = [root.location for root in self.external_roots]
        resources = [(item.location, item.path, item.access) for item in self.resources]
        if (
            roots != sorted(roots)
            or len(set(roots)) != len(roots)
            or len(set(resources)) != len(resources)
        ):
            raise ValueError("Workspace Snapshot包含重复根或资源")
        try:
            normalized_cwd = normalize_workspace_path(self.cwd, self.platform)
            normalized_resources = [
                normalize_workspace_path(item.path, self.platform) for item in self.resources
            ]
        except KernelError:
            raise ValueError("Workspace包含非法逻辑路径") from None
        if self.cwd != normalized_cwd or any(
            item.path != normalized
            for item, normalized in zip(self.resources, normalized_resources, strict=True)
        ):
            raise ValueError("Workspace包含非规范逻辑路径")
        comparison_keys = [
            (item.location, path_comparison_key(item.path, self.platform), item.access)
            for item in self.resources
        ]
        if len(set(comparison_keys)) != len(comparison_keys):
            raise ValueError("Workspace资源按平台语义重复")
        expected_order = sorted(
            self.resources,
            key=lambda item: (
                item.location,
                path_comparison_key(item.path, self.platform),
                item.access,
            ),
        )
        if list(self.resources) != expected_order:
            raise ValueError("Workspace资源顺序不规范")
        allowed = {"workspace", *roots}
        if any(item.location not in allowed for item in self.resources):
            raise ValueError("资源引用了未绑定的外部根")
        external_access = {root.location: set(root.access) for root in self.external_roots}
        if any(
            item.location != "workspace" and item.access not in external_access[item.location]
            for item in self.resources
        ):
            raise ValueError("资源访问模式超出外部根授权")
        cwd_observations = [
            item
            for item in self.resources
            if item.location == "workspace" and item.path == self.cwd and item.access == "read"
        ]
        if len(cwd_observations) != 1 or cwd_observations[0].kind != "directory":
            raise ValueError("Workspace Snapshot没有绑定cwd目录")
        if self.revision != workspace_snapshot_revision(self):
            raise ValueError("Workspace Snapshot revision不一致")
        return self


class WorkspaceLease(WorkspaceContract):
    spec_version: Literal["harnessix.workspace-lease/v1"] = "harnessix.workspace-lease/v1"
    workspace_id: Revision
    owner_id: str = Field(min_length=1, max_length=128)
    fencing_token: int = Field(ge=1)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


def workspace_snapshot_revision(snapshot: WorkspaceSnapshot) -> str:
    import hashlib
    import json

    payload = snapshot.model_dump(
        mode="json", exclude={"spec_version", "revision"}, warnings="error"
    )
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
