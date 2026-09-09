from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.contracts import (
    ProductConfigSnapshot,
    ProductConfigV1,
    ProductConfigV2,
    product_config_digest,
)
from harnessix.workspace.snapshot import SecureWorkspaceReader

MAX_PRODUCT_CONFIG_BYTES = 256 * 1024
MAX_PRODUCT_CONFIG_DEPTH = 32
MAX_PRODUCT_CONFIG_NODES = 20_000

type ProductConfigDocument = ProductConfigV1 | ProductConfigV2


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite number")


def _check_shape(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    nodes = [0] if count is None else count
    nodes[0] += 1
    if depth > MAX_PRODUCT_CONFIG_DEPTH or nodes[0] > MAX_PRODUCT_CONFIG_NODES:
        raise ValueError("JSON shape limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object key")
            _check_shape(item, depth=depth + 1, count=nodes)
    elif isinstance(value, list):
        for item in value:
            _check_shape(item, depth=depth + 1, count=nodes)
    elif not isinstance(value, str | int | float | bool | None) or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        raise ValueError("unsupported JSON scalar")


def decode_product_config_bytes(
    body: bytes, *, allow_legacy: bool = False
) -> ProductConfigDocument:
    if not body or len(body) > MAX_PRODUCT_CONFIG_BYTES or b"\x00" in body:
        raise KernelError("product_config_size", "产品配置为空或超过字节上限")
    try:
        text = body.decode("utf-8", errors="strict")
        raw = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
        _check_shape(raw)
        if not isinstance(raw, dict):
            raise ValueError("root must be object")
        version = raw.get("spec_version")
        if version == "harnessix.product-config/v2":
            # 使用 JSON 校验路径保留 strict 模式，同时允许 JSON Array 映射为不可变 Tuple。
            return ProductConfigV2.model_validate_json(text, strict=True)
        if allow_legacy and version == "harnessix.product-config/v1":
            return ProductConfigV1.model_validate_json(text, strict=True)
        raise ValueError("unsupported version")
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError, RecursionError):
        raise KernelError("product_config_invalid", "产品配置JSON或领域契约无效") from None


def _private_file(path: Path) -> tuple[int, ...] | None:
    if os.name != "posix":
        return None
    try:
        info = path.lstat()
    except OSError:
        raise KernelError("product_config_unavailable", "产品配置文件不可用") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise KernelError("product_config_permissions", "产品配置文件权限或身份不安全")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_product_config_bytes(path: str | Path) -> bytes:
    candidate = Path(path).absolute()
    if not candidate.name or candidate.name in {".", ".."}:
        raise KernelError("product_config_unavailable", "产品配置路径无效")
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("product_config_unavailable", "产品配置目录不可用") from None
    target = parent / candidate.name
    before = _private_file(target)
    try:
        with SecureWorkspaceReader(parent) as reader:
            body = reader.read_file(candidate.name, max_bytes=MAX_PRODUCT_CONFIG_BYTES)
    except KernelError as error:
        if error.code == "workspace_snapshot_limit":
            raise KernelError("product_config_size", "产品配置超过字节上限") from None
        raise KernelError("product_config_unavailable", "产品配置无法安全读取") from None
    after = _private_file(target)
    if before is not None and before != after:
        raise KernelError("product_config_changed", "产品配置在读取期间发生变化")
    return body


def load_product_config(
    path: str | Path, *, allow_legacy: bool = False
) -> ProductConfigSnapshot | ProductConfigV1:
    body = read_product_config_bytes(path)
    document = decode_product_config_bytes(body, allow_legacy=allow_legacy)
    if isinstance(document, ProductConfigV1):
        return document
    return ProductConfigSnapshot(
        source_sha256=hashlib.sha256(body).hexdigest(),
        config_sha256=product_config_digest(document),
        loaded_at=utc_now(),
        config=document,
    )


def canonical_product_config_bytes(config: ProductConfigV2) -> bytes:
    return (
        json.dumps(
            config.model_dump(mode="json", warnings="error"),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
