"""整组宿主调用绑定与公开效果；独立于旧单文件和 Agent v6 契约。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.patches.batch_approval_contracts import MAX_BATCH_PLAN_BYTES, ManagedPatchBatchPlan
from harnessix.patches.batch_contracts import MAX_BATCH_FILES
from harnessix.patches.batch_run_contracts import (
    BatchEffect,
    MemberEffect,
    StopReason,
    member_effect,
    ordered_progress,
)
from harnessix.patches.contracts import patch_path
from harnessix.patches.managed_contracts import PatchState
from harnessix.tools.contracts import ReadContract, Revision
from harnessix.tools.workspace import digest

BATCH_BRIDGE_POLICY = "managed-patch-batch-call/v1"
MAX_BATCH_CALL_BYTES = MAX_BATCH_PLAN_BYTES + 1024
MAX_BATCH_OUTPUT_BYTES = 48 * 1024


def batch_call_request_id(thread_id: UUID, turn_id: UUID, call_id: UUID, fingerprint: str) -> str:
    return digest((BATCH_BRIDGE_POLICY, str(thread_id), str(turn_id), str(call_id), fingerprint))


class ManagedPatchBatchCallPlan(ReadContract):
    version: Literal["managed-patch-batch-call/v1"] = "managed-patch-batch-call/v1"
    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    call_fingerprint: Revision
    backend: ManagedPatchBatchPlan
    approval_fingerprint: Revision

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.backend.request_id != batch_call_request_id(
            self.thread_id, self.turn_id, self.call_id, self.call_fingerprint
        ):
            raise ValueError("整组稳定请求与调用不一致")
        if self.approval_fingerprint != digest(
            self.model_dump(mode="json", exclude={"approval_fingerprint"})
        ):
            raise ValueError("整组调用审批指纹不一致")
        if len(self.model_dump_json().encode()) > MAX_BATCH_CALL_BYTES:
            raise ValueError("整组调用计划超过字节上限")
        return self


class BatchFileOutput(ReadContract):
    path: str = Field(min_length=1, max_length=1024)
    state: PatchState
    effect: MemberEffect
    before_sha256: Revision
    after_sha256: Revision

    _path = field_validator("path")(patch_path)

    @model_validator(mode="after")
    def validate_effect(self) -> Self:
        if self.state == "rejected" or self.effect != member_effect(self.state):
            raise ValueError("公开成员效果与状态不一致")
        return self


class ManagedPatchBatchOutput(ReadContract):
    version: Literal["managed-patch-batch-output/v1"] = "managed-patch-batch-output/v1"
    phase: Literal["not_started", "started", "finished"]
    stop_reason: StopReason | None = None
    effect: BatchEffect
    files: tuple[BatchFileOutput, ...] = Field(min_length=1, max_length=MAX_BATCH_FILES)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("公开成员路径重复")
        ordered_progress(tuple(f.state for f in self.files))
        effects = {f.effect for f in self.files}
        expected = (
            "unknown"
            if "unknown" in effects
            else "applied"
            if effects == {"applied"}
            else "partial"
            if "applied" in effects
            else "not_applied"
        )
        if self.effect != expected or (self.phase == "finished") != (self.stop_reason is not None):
            raise ValueError("公开整组效果或终止阶段不一致")
        if self.phase == "not_started" and any(f.state != "pending" for f in self.files):
            raise ValueError("未消费整组不能带成员执行事实")
        if self.stop_reason == "completed" and self.effect != "applied":
            raise ValueError("正常完成必须全部已应用")
        if len(self.model_dump_json().encode()) > MAX_BATCH_OUTPUT_BYTES:
            raise ValueError("公开整组结果超过字节上限")
        return self
