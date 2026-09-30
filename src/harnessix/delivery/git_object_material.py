"""固定Git对象的完整材料：纯合同与解码，不执行Git或登记备份。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.git_contracts import validate_git_object_id

GitObjectType = Literal["blob", "tree", "commit"]
GitObjectFormat = Literal["sha1", "sha256"]
_HEADER_BYTES = 128


def _invalid(code: str = "git_material_invalid") -> KernelError:
    """错误不包含对象正文、批次头、OID输入或第三方异常。"""
    return KernelError(code, "Git完整对象材料不符合固定读取契约")


@dataclass(frozen=True, slots=True)
class GitObjectRead:
    """明确固定类型、OID和格式；容量不是宿主可调的任意Process预算。"""

    object_type: GitObjectType
    object_id: str
    object_format: GitObjectFormat

    def __post_init__(self) -> None:
        if (
            type(self.object_type) is not str
            or self.object_type not in {"blob", "tree", "commit"}
            or type(self.object_format) is not str
            or self.object_format not in {"sha1", "sha256"}
            or type(self.object_id) is not str
        ):
            raise _invalid("git_material_request_invalid")
        try:
            validate_git_object_id(self.object_id)
            if len(self.object_id) != (40 if self.object_format == "sha1" else 64):
                raise ValueError
        except ValueError:
            raise _invalid("git_material_request_invalid") from None

    @property
    def max_body_bytes(self) -> int:
        return MAX_TRANSACTION_FILE_BYTES

    @property
    def stdout_limit(self) -> int:
        return self.max_body_bytes + _HEADER_BYTES + 1

    def binding(self) -> dict[str, JsonValue]:
        """批准覆盖具体对象用途，正文不进入计划或批准正文。"""
        return {
            "version": "git-object-read/v1",
            "object_type": self.object_type,
            "object_id": self.object_id,
            "object_format": self.object_format,
            "max_body_bytes": self.max_body_bytes,
            "stdout_limit": self.stdout_limit,
        }


@dataclass(frozen=True, slots=True)
class GitObjectMaterial:
    """原始正文经完整Git OID校验；不是CAS登记或业务成功凭证。"""

    object_type: GitObjectType
    object_id: str
    object_format: GitObjectFormat
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        request = GitObjectRead(self.object_type, self.object_id, self.object_format)
        if type(self.body) is not bytes or len(self.body) > request.max_body_bytes:
            raise _invalid("git_material_limit")
        digest = hashlib.new(request.object_format)
        digest.update(f"{request.object_type} {len(self.body)}\0".encode("ascii"))
        digest.update(self.body)
        if digest.hexdigest() != request.object_id:
            raise _invalid("git_material_oid_mismatch")

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def body_bytes(self) -> int:
        return len(self.body)


def decode_git_object_batch(request: GitObjectRead, framed: bytes) -> GitObjectMaterial:
    """仅接受一个完整batch对象；不把缺失、截断、尾随或错误类型补成材料。"""
    if type(request) is not GitObjectRead:
        raise _invalid("git_material_request_invalid")
    request.__post_init__()
    if type(framed) is not bytes:
        raise _invalid()
    if len(framed) > request.stdout_limit:
        raise _invalid("git_material_limit")
    end = framed.find(b"\n", 0, _HEADER_BYTES)
    if end < 0:
        raise _invalid()
    header = framed[:end]
    oid = request.object_id.encode("ascii")
    if framed == oid + b" missing\n":
        raise _invalid("git_material_missing")
    fields = header.split(b" ")
    if (
        len(fields) != 3
        or fields[0] != oid
        or fields[1] != request.object_type.encode("ascii")
        or not fields[2].isdigit()
        or (len(fields[2]) > 1 and fields[2].startswith(b"0"))
    ):
        raise _invalid()
    size = int(fields[2])
    if size > request.max_body_bytes:
        raise _invalid("git_material_limit")
    if len(framed) != end + 1 + size + 1 or not framed.endswith(b"\n"):
        raise _invalid()
    body = framed[end + 1 : -1]
    return GitObjectMaterial(request.object_type, request.object_id, request.object_format, body)
