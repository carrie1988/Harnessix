"""prepared 关联的完整规范 UTF-8 JSON；复用原编码器与 512KiB 物理记录预算。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import NoReturn

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config.git_delivery_plan_snapshot import invalid_git_delivery_plan
from harnessix.product_config.git_delivery_plan_wire import MAX_PRODUCT_GIT_PLAN_BYTES, _encode
from harnessix.product_config.git_prepared_link_contracts import (
    ProductGitPreparedLink,
    snapshot_product_git_prepared_link,
)


def encode_product_git_prepared_link(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """严格深层重建后沿原算法编码全部字段；不发布 Artifact 或授予执行权限。"""
    link = snapshot_product_git_prepared_link(value, checkpoint=checkpoint)
    # 原编码器只对确切 Core 类型排除自身指纹；关联封套保持完整正文和原预算。
    return _encode(link, checkpoint)


def decode_product_git_prepared_link(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitPreparedLink:
    """只接收规范 bytes；原解码器的有限类型分派不改动，不放宽旧 Schema。"""
    if type(checkpoint) is GitAuthenticationControl:
        with checkpoint.pure() as pure_check:
            return decode_product_git_prepared_link(body, checkpoint=pure_check)
    checkpoint()
    if type(body) is not bytes or not 1 <= len(body) <= MAX_PRODUCT_GIT_PLAN_BYTES:
        raise invalid_git_delivery_plan()
    callback_error: BaseException | None = None

    def check() -> None:
        nonlocal callback_error
        try:
            checkpoint()
        except BaseException as error:
            callback_error = error
            raise

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            check()
            if key in result:
                raise invalid_git_delivery_plan()
            result[key] = value
        return result

    def constant(_value: str) -> NoReturn:
        raise invalid_git_delivery_plan()

    try:
        # 与原 wire 相同的双遍解析：先拒绝重复键，再保留严格 JSON 的 UUID/日期/hex。
        json.loads(body.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant)
        check()
        parsed = ProductGitPreparedLink.model_validate_json(
            body, strict=True, context={"checkpoint": check}
        )
        snapshot = snapshot_product_git_prepared_link(parsed, checkpoint=check)
        # 原规范编码包含全部默认字段；漏字段、空白、转义或同义标量都不能补全后通过。
        if _encode(snapshot, check) != body:
            raise invalid_git_delivery_plan()
        check()
        return snapshot
    except (KernelError, ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        if callback_error is not None:
            raise callback_error from None
        raise invalid_git_delivery_plan() from None
