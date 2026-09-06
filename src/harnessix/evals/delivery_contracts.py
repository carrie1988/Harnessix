"""Coding Eval 通过结果到目标仓库的受控交付契约。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from harnessix.domain.models import ApprovalRecord
from harnessix.evals.contracts import EvalContract, EvalRepository, _relative_path
from harnessix.tools.workspace import digest

CHANGE_PACKAGE_SPEC_VERSION: Literal["harnessix.coding-eval-change-package/v1"] = (
    "harnessix.coding-eval-change-package/v1"
)
DELIVERY_PLAN_SPEC_VERSION: Literal["harnessix.coding-eval-delivery-plan/v1"] = (
    "harnessix.coding-eval-delivery-plan/v1"
)
DELIVERY_RECORD_SPEC_VERSION: Literal["harnessix.coding-eval-delivery-record/v1"] = (
    "harnessix.coding-eval-delivery-record/v1"
)
MAX_CHANGE_IMAGE_BYTES = 1024 * 1024

DeliveryStatus = Literal[
    "pending_approval",
    "approved",
    "rejected",
    "applying",
    "applied",
    "conflicted",
    "unknown",
]


class CodingEvalChangeImage(EvalContract):
    """单个已有UTF-8普通文件的完整镜像。"""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    utf8_bytes: int = Field(ge=0, le=MAX_CHANGE_IMAGE_BYTES)
    text: str = Field(repr=False)

    @model_validator(mode="after")
    def content_matches_evidence(self) -> Self:
        try:
            body = self.text.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("变更镜像必须是合法UTF-8") from None
        if len(body) != self.utf8_bytes or hashlib.sha256(body).hexdigest() != self.sha256:
            raise ValueError("变更镜像与大小或摘要不一致")
        return self


class CodingEvalChangePackage(EvalContract):
    """由严格通过的单文件Eval报告及工作区实况生成的私有交付包。"""

    spec_version: Literal["harnessix.coding-eval-change-package/v1"] = CHANGE_PACKAGE_SPEC_VERSION
    run_id: UUID
    task_id: str = Field(min_length=1, max_length=128)
    task_version: int = Field(ge=1)
    task_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repository: EvalRepository
    source_tree_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    path: str = Field(min_length=1, max_length=1024)
    source_mode: int
    before: CodingEvalChangeImage
    after: CodingEvalChangeImage
    workspace_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("source_mode")
    @classmethod
    def supported_mode(cls, value: int) -> int:
        if type(value) is not int or value not in {0o644, 0o755}:
            raise ValueError("交付包只支持0644或0755普通文件")
        return value

    @model_validator(mode="after")
    def package_is_consistent(self) -> Self:
        if self.before.sha256 == self.after.sha256:
            raise ValueError("交付包必须包含实际内容变化")
        expected = digest(self.model_dump(mode="json", exclude={"package_fingerprint"}))
        if self.package_fingerprint != expected:
            raise ValueError("交付包指纹不一致")
        return self


class CodingEvalDeliveryPlan(EvalContract):
    """目标仓库无副作用预检产生、供人工批准绑定的不可变计划。"""

    spec_version: Literal["harnessix.coding-eval-delivery-plan/v1"] = DELIVERY_PLAN_SPEC_VERSION
    delivery_id: UUID
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=128)
    task_version: int = Field(ge=1)
    target_repository: EvalRepository
    target_scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_tree_oid: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    path: str = Field(min_length=1, max_length=1024)
    source_mode: int
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    approval_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("source_mode")
    @classmethod
    def supported_mode(cls, value: int) -> int:
        if type(value) is not int or value not in {0o644, 0o755}:
            raise ValueError("交付计划只支持0644或0755普通文件")
        return value

    @model_validator(mode="after")
    def plan_is_consistent(self) -> Self:
        if self.before_sha256 == self.after_sha256:
            raise ValueError("交付计划必须包含实际内容变化")
        expected = digest(self.model_dump(mode="json", exclude={"approval_fingerprint"}))
        if self.approval_fingerprint != expected:
            raise ValueError("交付批准指纹不一致")
        return self


class CodingEvalDeliveryTransition(EvalContract):
    sequence: int = Field(ge=1, le=64)
    from_status: DeliveryStatus | None
    to_status: DeliveryStatus
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    occurred_at: AwareDatetime


class CodingEvalDeliveryRecord(EvalContract):
    """原子重写的完整交付状态与转换历史，不保存目标绝对路径。"""

    spec_version: Literal["harnessix.coding-eval-delivery-record/v1"] = DELIVERY_RECORD_SPEC_VERSION
    delivery_id: UUID
    plan: CodingEvalDeliveryPlan
    status: DeliveryStatus
    approval: ApprovalRecord | None = Field(default=None, repr=False)
    temporary_identity: tuple[int, int] | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    transitions: tuple[CodingEvalDeliveryTransition, ...] = Field(min_length=1, max_length=64)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    record_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_is_consistent(self) -> Self:
        if self.delivery_id != self.plan.delivery_id:
            raise ValueError("交付记录与计划身份不一致")
        if self.updated_at < self.created_at:
            raise ValueError("交付更新时间不能早于创建时间")
        if self.plan.created_at != self.created_at:
            raise ValueError("交付计划与记录创建时间不一致")
        if any(item.sequence != index for index, item in enumerate(self.transitions, start=1)):
            raise ValueError("交付状态序列不连续")
        allowed: dict[DeliveryStatus, set[DeliveryStatus]] = {
            "pending_approval": {"approved", "rejected"},
            "approved": {"applying"},
            "applying": {"approved", "applied", "conflicted", "unknown"},
            "rejected": set(),
            "applied": set(),
            "conflicted": set(),
            "unknown": set(),
        }
        for index, item in enumerate(self.transitions):
            expected_from = None if index == 0 else self.transitions[index - 1].to_status
            if item.from_status != expected_from:
                raise ValueError("交付状态转换链不连续")
            if index == 0:
                if item.to_status != "pending_approval":
                    raise ValueError("交付首个状态必须等待批准")
            elif expected_from is None or item.to_status not in allowed[expected_from]:
                raise ValueError("交付状态转换不受支持")
            if index and item.occurred_at < self.transitions[index - 1].occurred_at:
                raise ValueError("交付状态转换时间倒退")
        if self.transitions[-1].to_status != self.status:
            raise ValueError("交付状态与最后转换不一致")
        first_at = self.transitions[0].occurred_at
        last_at = self.transitions[-1].occurred_at
        if self.created_at != first_at or self.updated_at != last_at:
            raise ValueError("交付记录时间与转换历史不一致")
        if (self.approval is None) != (self.status == "pending_approval"):
            raise ValueError("交付批准决定与状态不一致")
        if self.approval is not None:
            if self.approval.request_fingerprint != self.plan.approval_fingerprint:
                raise ValueError("交付决定未绑定批准指纹")
            if (self.approval.outcome.value == "rejected") != (self.status == "rejected"):
                if self.approval.outcome.value == "rejected":
                    raise ValueError("拒绝决定不能进入写执行状态")
        if (self.status == "applying") != (self.temporary_identity is not None):
            raise ValueError("只有执行中状态必须保存临时文件身份")
        if self.temporary_identity is not None and any(
            type(value) is not int or value < 0 for value in self.temporary_identity
        ):
            raise ValueError("交付临时文件身份无效")
        expected = digest(self.model_dump(mode="json", exclude={"record_fingerprint"}))
        if self.record_fingerprint != expected:
            raise ValueError("交付记录指纹不一致")
        return self


def change_image(body: bytes) -> CodingEvalChangeImage:
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("变更镜像必须是合法UTF-8") from None
    return CodingEvalChangeImage(
        sha256=hashlib.sha256(body).hexdigest(),
        utf8_bytes=len(body),
        text=text,
    )


def transition(
    sequence: int,
    from_status: DeliveryStatus | None,
    to_status: DeliveryStatus,
    reason: str,
    occurred_at: datetime,
) -> CodingEvalDeliveryTransition:
    return CodingEvalDeliveryTransition(
        sequence=sequence,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        occurred_at=occurred_at,
    )
