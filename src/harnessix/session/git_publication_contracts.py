"""Git 业务记录的有限来源声明；不包含正文、密钥、审批或恢复执行权限。"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from harnessix.agent.errors import KernelError
from harnessix.session.publication_seal import Digest


class GitDeliveryRecordClaims(BaseModel):
    """绑定一条新事实的业务身份和前缀位置；不是跨库归属或完整尾锚证明。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    delivery_id: UUID
    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    route_id: UUID
    record_id: UUID
    publication_epoch: UUID
    record_kind: Literal[
        "object_inventory", "product_link", "worktree_event", "checkpoint", "commit_event"
    ]
    sequence: Annotated[int, Field(ge=1, le=2**63 - 1)]
    previous_sha256: Digest

    @model_validator(mode="before")
    @classmethod
    def original_types(cls, value: object, info: ValidationInfo) -> object:
        """Python输入先查实际类型；JSON UUID仍按原严格JSON合同解码。"""
        if info.mode == "python":
            if type(value) is not dict:
                raise ValueError("Git记录声明类型无效")
            identities = (
                "delivery_id",
                "thread_id",
                "turn_id",
                "call_id",
                "route_id",
                "record_id",
                "publication_epoch",
            )
            if any(type(value.get(name)) is not UUID for name in identities) or (
                type(value.get("sequence")) is not int
                or type(value.get("record_kind")) is not str
                or type(value.get("previous_sha256")) is not str
            ):
                raise ValueError("Git记录声明字段类型无效")
        return value

    @model_validator(mode="after")
    def initial_prefix(self) -> Self:
        """仅第一条允许空前缀；前缀内容和独立尾锚仍由账本读写端核验。"""
        if (self.sequence == 1) != (self.previous_sha256 == "0" * 64):
            raise ValueError("Git记录序号与前缀不一致")
        return self


def snapshot_git_delivery_claims(value: object) -> GitDeliveryRecordClaims:
    """重新建立严格快照，不信任未经校验的 model_copy 或 model_construct 值。"""
    if type(value) is not GitDeliveryRecordClaims:
        raise KernelError("publication_history_unproven", "Git记录缺少匹配的来源认证")
    try:
        fields = GitDeliveryRecordClaims.model_fields
        if (
            len(value.__dict__) != len(fields)
            or set(value.__dict__) != set(fields)
            or value.__pydantic_extra__ is not None
        ):
            raise ValueError("Git记录声明字段集合无效")
        # 先取原字段而非序列化，避免Pydantic把标量子类先规范化后绕过实际类型检查。
        return GitDeliveryRecordClaims.model_validate(
            {name: getattr(value, name) for name in fields}
        )
    except Exception:
        raise KernelError("publication_history_unproven", "Git记录缺少匹配的来源认证") from None
