"""Product Action配置的安全读取、严格解码与不可变快照构造。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.action_contracts import (
    ProductActionConfigSnapshot,
    ProductActionConfigSource,
    ProductActionConfigV1,
    build_product_action_config,
)
from harnessix.product_config.codec import (
    BoundedConfigSizeError,
    decode_bounded_config_json,
    read_product_config_bytes,
)


def decode_product_action_config_bytes(body: bytes) -> ProductActionConfigV1:
    """严格解析单一v1 Action配置；错误只暴露稳定、脱敏代码。"""

    try:
        text, raw = decode_bounded_config_json(body)
    except BoundedConfigSizeError:
        raise KernelError(
            "product_action_config_size", "Product Action配置为空或超过字节上限"
        ) from None
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise KernelError(
            "product_action_config_invalid", "Product Action配置JSON或领域契约无效"
        ) from None
    try:
        if raw.get("spec_version") != "harnessix.product-action-config/v1":
            raise ValueError("unsupported version")
        return ProductActionConfigV1.model_validate_json(text, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise KernelError(
            "product_action_config_invalid", "Product Action配置JSON或领域契约无效"
        ) from None


def canonical_product_action_config_bytes(config: ProductActionConfigV1) -> bytes:
    """返回稳定、可审计且不含Secret值的Action配置JSON。"""

    checked = ProductActionConfigV1.model_validate_json(config.model_dump_json(warnings="error"))
    return (
        json.dumps(
            checked.model_dump(mode="json", warnings="error"),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def product_action_config_snapshot(
    config: ProductActionConfigV1,
    *,
    source_kind: ProductActionConfigSource = "builtin",
    source_bytes: bytes | None = None,
) -> ProductActionConfigSnapshot:
    """从已验证配置构造来源绑定快照；内建来源使用规范JSON作为源字节。"""

    checked = ProductActionConfigV1.model_validate_json(config.model_dump_json(warnings="error"))
    body = (
        source_bytes if source_bytes is not None else canonical_product_action_config_bytes(checked)
    )
    return ProductActionConfigSnapshot(
        source_kind=source_kind,
        source_sha256=hashlib.sha256(body).hexdigest(),
        config_sha256=checked.config_sha256,
        loaded_at=utc_now(),
        config=checked,
    )


def load_product_action_config(path: str | Path | None) -> ProductActionConfigSnapshot:
    """安全加载外部Action配置；省略路径时返回版本化内建配置快照。"""

    if path is None:
        return product_action_config_snapshot(build_product_action_config())
    try:
        body = read_product_config_bytes(path)
    except KernelError as error:
        mappings = {
            "product_config_size": "product_action_config_size",
            "product_config_unavailable": "product_action_config_unavailable",
            "product_config_permissions": "product_action_config_permissions",
            "product_config_changed": "product_action_config_changed",
        }
        code = mappings.get(error.code, "product_action_config_unavailable")
        raise KernelError(code, "Product Action配置文件无法安全读取") from None
    config = decode_product_action_config_bytes(body)
    return product_action_config_snapshot(config, source_kind="file", source_bytes=body)
