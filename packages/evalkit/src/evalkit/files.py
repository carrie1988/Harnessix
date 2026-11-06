"""证据文件的有界读取与私有原子写入：0600、fsync后替换、不跟随符号链接。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from evalkit.contracts import ArtifactRef
from evalkit.digest import bytes_digest
from evalkit.errors import EvalkitError


def read_bytes(path: Path, max_bytes: int, *, require_private: bool = False) -> bytes:
    """只读普通文件；超过上限、是链接或（按需）权限不是0600时拒绝。"""
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > max_bytes
            or (require_private and info.st_mode & 0o777 != 0o600)
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > max_bytes:
            raise OSError
        return body
    except OSError:
        raise EvalkitError("file_invalid", "证据文件缺失、不是普通文件或超过上限") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_model[T: BaseModel](
    path: Path, model: type[T], max_bytes: int, *, require_private: bool = False
) -> T:
    body = read_bytes(path, max_bytes, require_private=require_private)
    try:
        return model.model_validate_json(body, strict=True)
    except (ValueError, ValidationError):
        raise EvalkitError("file_invalid", "证据文件不符合合同") from None


def read_json_object(path: Path, max_bytes: int) -> dict[str, Any]:
    """读取外部系统的原始JSON对象；只解析，不解释字段。"""
    try:
        value = json.loads(read_bytes(path, max_bytes))
    except (ValueError, RecursionError):
        raise EvalkitError("file_invalid", "证据文件不是有效JSON") from None
    if not isinstance(value, dict):
        raise EvalkitError("file_invalid", "证据文件必须是JSON对象")
    return value


def write_model(path: Path, model: BaseModel, max_bytes: int) -> None:
    """在目标目录内写0600临时文件，fsync后原子替换；目标是符号链接时拒绝。"""
    body = (model.model_dump_json(indent=2) + "\n").encode()
    if len(body) > max_bytes:
        raise EvalkitError("file_too_large", "输出超过大小上限")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        parent = path.parent.resolve(strict=True)
        target = parent / path.name
        if target.is_symlink():
            raise EvalkitError("file_path_denied", "输出目标不能是符号链接")
        temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
        temporary = None
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except EvalkitError:
        raise
    except OSError:
        raise EvalkitError("file_write_failed", "输出写入失败") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def artifact_ref(name: str, path: Path, evidence_root: Path, max_bytes: int) -> ArtifactRef:
    """登记证据库内的冻结制品；路径必须位于证据库根目录下且不是链接。"""
    root = evidence_root.resolve(strict=True)
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise EvalkitError("artifact_outside_root", "制品不在证据库根目录内")
    body = read_bytes(path, max_bytes)
    return ArtifactRef(
        name=name, digest=bytes_digest(body), path=path.resolve().relative_to(root).as_posix()
    )


def read_artifact(ref: ArtifactRef, evidence_root: Path, max_bytes: int) -> bytes:
    """按登记路径读取制品并核对摘要；内容变化即拒绝。"""
    root = evidence_root.resolve(strict=True)
    path = root / ref.path
    if not path.resolve(strict=False).is_relative_to(root):
        raise EvalkitError("artifact_outside_root", "制品不在证据库根目录内")
    body = read_bytes(path, max_bytes)
    if bytes_digest(body) != ref.digest:
        raise EvalkitError("artifact_digest_mismatch", "制品内容与登记摘要不一致")
    return body
