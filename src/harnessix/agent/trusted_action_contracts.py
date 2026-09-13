"""Agent Session持久化的Trusted Action审批与效果投影。"""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import ApprovalOutcome, ApprovalRecord, ContractModel
from harnessix.tools.contracts import Revision


class TrustedActionEffect(ContractModel):
    """Session内的有界Trusted Action终态；完整正文只由Artifact保存。"""

    spec_version: Literal["harnessix.agent-trusted-action-effect/v1"] = (
        "harnessix.agent-trusted-action-effect/v1"
    )
    plan_id: UUID
    plan_fingerprint: Revision
    state: Literal["succeeded", "failed", "unknown", "manual_intervention"]
    origin: Literal["execution", "recovery"]
    artifact_sha256: Revision | None = None


class TrustedActionApprovalRequestContent(ContractModel):
    """Router权威计划在Agent Session中的最小审批投影。"""

    kind: Literal["trusted_action_approval_request"] = "trusted_action_approval_request"
    approval_id: UUID
    call_id: UUID
    presentation: Literal["tool", "patch_batch", "process"]
    plan_id: UUID
    plan_fingerprint: Revision
    execution_fingerprint: Revision
    request_fingerprint: Revision
    policy_id: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=128)
    route_state: Literal[
        "pending_approval",
        "ready",
        "denied",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "reconciling",
        "manual_intervention",
    ] = "pending_approval"
    decision: ApprovalRecord | None = None
    diff_artifact: ArtifactRef | None = None

    @model_serializer(mode="wrap")
    def serialize_request(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.diff_artifact is None:
            data.pop("diff_artifact", None)
        return data

    @model_validator(mode="after")
    def bound_projection(self) -> Self:
        if self.presentation == "patch_batch" and self.diff_artifact is None:
            raise ValueError("Patch审批必须携带完整Diff Artifact")
        if self.presentation == "process" and self.diff_artifact is not None:
            raise ValueError("Process审批不能携带Diff Artifact")
        if self.decision is None:
            if self.route_state != "pending_approval":
                raise ValueError("未决定审批只能投影pending_approval")
        elif self.decision.request_fingerprint != self.request_fingerprint or (
            self.decision.outcome is ApprovalOutcome.REJECTED
        ) != (self.route_state == "denied"):
            raise ValueError("Trusted Action决定与请求或Router状态不一致")
        if len(self.model_dump_json().encode()) > 16_384:
            raise ValueError("Trusted Action审批投影超过字节上限")
        return self


class TrustedActionReview(ContractModel):
    """审批前生成的有界可见证据；Gateway不直接依赖Artifact子系统。"""

    diff_artifact: ArtifactRef | None = None
