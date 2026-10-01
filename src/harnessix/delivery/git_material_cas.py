"""完整 Git 对象与原 Workspace CAS 的类型绑定，不提供业务认证或发布权限。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Any, Final, Literal, cast

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_object_material import (
    GitObjectFormat,
    GitObjectMaterial,
    GitObjectRead,
    GitObjectType,
)
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore

_VERSION: Final = "harnessix.git-object-material-reference/v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _invalid_reference() -> KernelError:
    return KernelError("git_material_cas_reference_invalid", "Git对象材料CAS引用无效")


@dataclass(frozen=True, slots=True)
class GitObjectMaterialReference:
    """只有对象与完整正文的内容绑定；普通摘要不是原 Owner 或业务认证证明。"""

    object_type: GitObjectType
    object_id: str
    object_format: GitObjectFormat
    body_sha256: str
    body_bytes: int
    cas_digest: str
    version: Literal["harnessix.git-object-material-reference/v1"] = _VERSION

    def __post_init__(self) -> None:
        if (
            type(self.version) is not str
            or self.version != _VERSION
            or type(self.body_bytes) is not int
            or not 0 <= self.body_bytes <= MAX_TRANSACTION_FILE_BYTES
            or type(self.body_sha256) is not str
            or _DIGEST.fullmatch(self.body_sha256) is None
            or type(self.cas_digest) is not str
            or self.cas_digest != self.body_sha256
        ):
            raise _invalid_reference()
        try:
            GitObjectRead(self.object_type, self.object_id, self.object_format)
        except KernelError:
            raise _invalid_reference() from None

    def binding(self) -> dict[str, JsonValue]:
        """重新核验后返回七个固定字段，不扩展角色、批准或历史边界。"""
        reference = _reference_snapshot(self)
        return {
            "version": reference.version,
            "object_type": reference.object_type,
            "object_id": reference.object_id,
            "object_format": reference.object_format,
            "body_sha256": reference.body_sha256,
            "body_bytes": reference.body_bytes,
            "cas_digest": reference.cas_digest,
        }

    @classmethod
    def from_binding(cls, value: object) -> GitObjectMaterialReference:
        """严格读取规范字段；不接受额外字段、默认补字段或隐式类型转换。"""
        if (
            cls is not GitObjectMaterialReference
            or type(value) is not dict
            or any(type(key) is not str for key in value)
            or set(value) != {item.name for item in fields(cls)}
        ):
            raise _invalid_reference()
        # 动态字典只在此边界进入；正常构造器逐字段校验实际类型。
        return cls(**cast(dict[str, Any], value))


def _reference_snapshot(reference: GitObjectMaterialReference) -> GitObjectMaterialReference:
    if type(reference) is not GitObjectMaterialReference:
        raise _invalid_reference()
    try:
        return GitObjectMaterialReference(
            reference.object_type,
            reference.object_id,
            reference.object_format,
            reference.body_sha256,
            reference.body_bytes,
            reference.cas_digest,
            reference.version,
        )
    except (AttributeError, KernelError):
        raise _invalid_reference() from None


def _material_snapshot(material: GitObjectMaterial) -> GitObjectMaterial:
    if type(material) is not GitObjectMaterial:
        raise KernelError("git_material_cas_invalid", "Git对象材料CAS输入无效")
    try:
        return GitObjectMaterial(
            material.object_type, material.object_id, material.object_format, material.body
        )
    except (AttributeError, KernelError):
        raise KernelError("git_material_cas_invalid", "Git对象材料CAS输入无效") from None


@dataclass(frozen=True, slots=True)
class GitMaterialCAS:
    """只复用原 Store 的完整 Blob IO；不登记事务、GitDB或产品成功。"""

    store: SQLiteWorkspaceTransactionStore = field(repr=False)

    def persist(self, material: GitObjectMaterial) -> GitObjectMaterialReference:
        """验真实际材料，写原 CAS 并完整回读，回读成功后才返回类型引用。"""
        snapshot = _material_snapshot(material)
        digest = snapshot.body_sha256
        reference = GitObjectMaterialReference(
            snapshot.object_type,
            snapshot.object_id,
            snapshot.object_format,
            digest,
            snapshot.body_bytes,
            digest,
        )
        try:
            self.store.put_blob(reference.cas_digest, snapshot.body)
            self.read(reference)
        except Exception:
            # 原生IO及底层错误只转换为固定公开错误，不暴露材料或第三方异常。
            raise KernelError("git_material_cas_write_failed", "Git对象材料CAS持久化失败") from None
        return reference

    def read(self, reference: GitObjectMaterialReference) -> GitObjectMaterial:
        """重验引用并从原 CAS 完整读取，核对长度、正文 SHA 和带真实类型的 Git OID。"""
        snapshot = _reference_snapshot(reference)
        try:
            body = self.store.blob(snapshot.cas_digest)
            material = GitObjectMaterial(
                snapshot.object_type, snapshot.object_id, snapshot.object_format, body
            )
            if (
                material.body_bytes != snapshot.body_bytes
                or material.body_sha256 != snapshot.body_sha256
            ):
                raise KernelError("git_material_cas_read_failed", "Git对象材料CAS读取失败")
        except Exception:
            raise KernelError("git_material_cas_read_failed", "Git对象材料CAS读取失败") from None
        return material
