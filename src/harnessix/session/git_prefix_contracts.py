"""独立Git尾锚的有限声明；验证来源标记不属于持久身份或执行授权。"""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID
from weakref import ReferenceType, ref

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, model_validator

from harnessix.agent.errors import KernelError


class GitStorePrefixAnchorClaims(BaseModel):
    """仅绑定v2、原Store genesis epoch和revision，不证明完整catalog或当前Root归属。"""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )

    schema_version: Literal["2"]
    store_genesis_epoch: UUID
    revision: Annotated[int, Field(ge=0, le=2**63 - 1)]
    _validation: tuple[ReferenceType[GitStorePrefixAnchorClaims], tuple[str, UUID, int]] | None = (
        PrivateAttr(default=None)
    )

    @model_validator(mode="before")
    @classmethod
    def original_types(cls, value: object, info: ValidationInfo) -> object:
        """Python字段必须为精确原生标量；JSON UUID仅在正式JSON校验中解码。"""
        if info.mode == "python" and (
            type(value) is not dict
            or type(value.get("schema_version")) is not str
            or type(value.get("store_genesis_epoch")) is not UUID
            or type(value.get("revision")) is not int
        ):
            raise ValueError("Git尾锚声明字段类型无效")
        return value

    @model_validator(mode="after")
    def validated_instance(self) -> Self:
        """仅正式校验建立实例与字段见证；construct不运行，浅/深copy仍指向原实例。"""
        self._validation = (
            ref(self),
            (self.schema_version, self.store_genesis_epoch, self.revision),
        )
        return self

    def __eq__(self, other: object) -> bool:
        """声明按合同字段比较；私有校验见证不是声明内容。"""
        if not isinstance(other, GitStorePrefixAnchorClaims) or type(self) is not type(other):
            return False
        return (
            self.schema_version,
            self.store_genesis_epoch,
            self.revision,
        ) == (other.schema_version, other.store_genesis_epoch, other.revision)


def snapshot_git_store_prefix_claims(value: object) -> GitStorePrefixAnchorClaims:
    """只从实际校验过的精确实例建立深快照，不接纳construct/copy/子类或额外字段。"""
    if type(value) is not GitStorePrefixAnchorClaims:
        raise KernelError("publication_history_unproven", "Git尾锚缺少匹配的来源认证")
    try:
        fields = GitStorePrefixAnchorClaims.model_fields
        validation = value._validation
        if (
            validation is None
            or validation[0]() is not value
            or type(value.__dict__) is not dict
            or set(value.__dict__) != set(fields)
            or value.__pydantic_extra__ is not None
            or (value.schema_version, value.store_genesis_epoch, value.revision) != validation[1]
        ):
            raise ValueError("Git尾锚声明未实际校验或已被变造")
        # 保留原标量再校验，不能先序列化后把子类或错误类型规范化为合法字段。
        return GitStorePrefixAnchorClaims.model_validate(
            {name: getattr(value, name) for name in fields}
        )
    except Exception:
        raise KernelError("publication_history_unproven", "Git尾锚缺少匹配的来源认证") from None
