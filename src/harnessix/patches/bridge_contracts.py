"""宿主调用绑定契约；不属于 Agent v5 事件，也不是模型输入。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.patches.contracts import PatchManifest, patch_path
from harnessix.patches.managed_contracts import PatchState
from harnessix.tools.contracts import ReadContract, Revision
from harnessix.tools.workspace import digest

BRIDGE_POLICY = "managed-patch-call/v1"


def call_request_id(thread_id: UUID, turn_id: UUID, call_id: UUID, fingerprint: str) -> str:
    return digest((BRIDGE_POLICY, str(thread_id), str(turn_id), str(call_id), fingerprint))


class ManagedPatchCallPlan(ReadContract):
    version: Literal["managed-patch-call/v1"] = "managed-patch-call/v1"
    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    call_fingerprint: Revision
    request_id: Revision
    workspace_id: UUID
    plan_id: UUID
    manifest: PatchManifest
    backend_fingerprint: Revision
    approval_fingerprint: Revision

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.request_id != call_request_id(
            self.thread_id, self.turn_id, self.call_id, self.call_fingerprint
        ):
            raise ValueError("Patch 稳定请求与调用归属不一致")
        if self.backend_fingerprint != digest(
            (str(self.workspace_id), str(self.plan_id), self.request_id, self.manifest.fingerprint)
        ):
            raise ValueError("Patch 后端指纹与计划不一致")
        if self.approval_fingerprint != digest(
            self.model_dump(mode="json", exclude={"approval_fingerprint"})
        ):
            raise ValueError("Patch 审批指纹与调用计划不一致")
        return self


class ManagedPatchOutput(ReadContract):
    version: Literal["managed-patch-output/v1"] = "managed-patch-output/v1"
    path: str = Field(min_length=1, max_length=1024)
    state: PatchState
    before_sha256: Revision
    after_sha256: Revision

    _path = field_validator("path")(patch_path)
