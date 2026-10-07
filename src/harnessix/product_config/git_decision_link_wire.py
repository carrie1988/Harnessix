"""Git 决定声明的严格规范字节，沿用完整编码器与原 512KiB 物理预算。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import NoReturn

from pydantic import TypeAdapter, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_decision_link_contracts import (
    ProductGitApprovedLink,
    ProductGitCancelledLink,
    ProductGitDecisionLink,
    ProductGitDeniedLink,
    snapshot_product_git_decision_link,
)
from harnessix.product_config.git_delivery_plan_snapshot import invalid_git_delivery_plan
from harnessix.product_config.git_delivery_plan_wire import MAX_PRODUCT_GIT_PLAN_BYTES, _encode

_DECISION_ADAPTER: TypeAdapter[
    ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink
] = TypeAdapter(ProductGitDecisionLink)


def encode_product_git_decision_link(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """完整重建后规范编码；不生成 MAC、不追加事件，也不产生授权。"""
    declaration = snapshot_product_git_decision_link(value, checkpoint=checkpoint)
    return _encode(declaration, checkpoint)


def decode_product_git_decision_link(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitApprovedLink | ProductGitDeniedLink | ProductGitCancelledLink:
    """双遍严格解析及原字节比对；未知变体、缺省补全和非规范字节均拒绝。"""
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
        json.loads(body.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant)
        check()
        parsed = _DECISION_ADAPTER.validate_json(body, strict=True, context={"checkpoint": check})
        declaration = snapshot_product_git_decision_link(parsed, checkpoint=check)
        if _encode(declaration, check) != body:
            raise invalid_git_delivery_plan()
        check()
        return declaration
    except (KernelError, ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        if callback_error is not None:
            raise callback_error from None
        raise invalid_git_delivery_plan() from None
