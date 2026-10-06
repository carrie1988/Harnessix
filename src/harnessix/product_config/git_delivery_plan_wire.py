"""Git 产品计划的完整规范 UTF-8 JSON；保持原物理记录 512KiB 上限。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import NoReturn, cast

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitDeliveryCore,
    ProductGitDeliveryPlan,
)
from harnessix.product_config.git_delivery_plan_snapshot import (
    invalid_git_delivery_plan,
    snapshot_product_git_delivery_core,
    snapshot_product_git_delivery_core_v2,
    snapshot_product_git_delivery_plan,
    snapshot_product_git_delivery_plan_v2,
)

MAX_PRODUCT_GIT_PLAN_BYTES = 512 * 1024


def _encode(
    plan: ProductGitDeliveryPlan
    | ProductGitDeliveryCore
    | ProductGitDeliveryCoreV2
    | ProductGitDeliveryPlanV2,
    checkpoint: Callable[[], None],
) -> bytes:
    """完整逐块编码；超限或取消不返回任何部分记录，不提高现有账本预算。"""
    checkpoint()
    # Core 的原内容地址只排除自身指纹；嵌套指纹及全部事实仍完整保留。
    exclude = (
        {"fingerprint"}
        if type(plan) in {ProductGitDeliveryCore, ProductGitDeliveryCoreV2}
        else set()
    )
    payload = plan.model_dump(mode="json", exclude=exclude, warnings="error")
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    result = bytearray()
    iterator = encoder.iterencode(payload)
    while True:
        checkpoint()
        try:
            part = next(iterator)
        except StopIteration:
            break
        except (ValueError, TypeError, RecursionError):
            raise invalid_git_delivery_plan() from None
        for offset in range(0, len(part), 16384):
            checkpoint()
            try:
                encoded = part[offset : offset + 16384].encode("utf-8", "strict")
            except UnicodeError:
                raise invalid_git_delivery_plan() from None
            result.extend(encoded)
            if len(result) > MAX_PRODUCT_GIT_PLAN_BYTES:
                raise invalid_git_delivery_plan()
    checkpoint()
    return bytes(result)


def encode_product_git_delivery_plan(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """先完整深层重建再编码；不是认证发布器，不签发任何授权。"""
    plan = snapshot_product_git_delivery_plan(value, checkpoint=checkpoint)
    return _encode(plan, checkpoint)


def decode_product_git_delivery_plan(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryPlan:
    """拒绝重复键、额外字段、大小写别名、缺省补全及任何非规范同义字节。"""
    return _decode(body, ProductGitDeliveryPlan, checkpoint)


def encode_product_git_delivery_core(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """原编码算法保存完整 Core，不含自身指纹；仍沿用原 512KiB 上限。"""
    core = snapshot_product_git_delivery_core(value, checkpoint=checkpoint)
    return _encode(core, checkpoint)


def decode_product_git_delivery_core(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryCore:
    """从完整规范字节恢复自身内容地址；成功不证明 MAC、Owner 或原 Session。"""
    return _decode(body, ProductGitDeliveryCore, checkpoint)


def _decode[
    T: ProductGitDeliveryPlan
    | ProductGitDeliveryCore
    | ProductGitDeliveryCoreV2
    | ProductGitDeliveryPlanV2
](body: object, kind: type[T], checkpoint: Callable[[], None]) -> T:
    """两代有限模型共用原严格 JSON 算法，不接受调用方解析器或验证器。"""
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
        # 第一遍拒绝重复键，第二遍由原 Pydantic 严格 JSON 模式保留 UUID/日期语义。
        payload = json.loads(
            body.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=constant
        )
        check()
        if kind is ProductGitDeliveryCore or kind is ProductGitDeliveryCoreV2:
            if type(payload) is not dict or "fingerprint" in payload or not body.startswith(b"{"):
                raise invalid_git_delivery_plan()
            # 只注入由完整原始正文计算的自身指纹，不默认补全任何持久字段。
            # 保留原始 JSON 字节，让严格 JSON 模式负责 UUID、日期及 hex 解码。
            digest = hashlib.sha256(body).hexdigest().encode("ascii")
            full = b'{"fingerprint":"' + digest + b'",' + body[1:]
            parsed = kind.model_validate_json(full, context={"checkpoint": check})
        else:
            parsed = kind.model_validate_json(body, context={"checkpoint": check})
        snapshot: (
            ProductGitDeliveryCore
            | ProductGitDeliveryCoreV2
            | ProductGitDeliveryPlan
            | ProductGitDeliveryPlanV2
        )
        if kind is ProductGitDeliveryCore:
            snapshot = snapshot_product_git_delivery_core(parsed, checkpoint=check)
        elif kind is ProductGitDeliveryCoreV2:
            snapshot = snapshot_product_git_delivery_core_v2(parsed, checkpoint=check)
        elif kind is ProductGitDeliveryPlan:
            snapshot = snapshot_product_git_delivery_plan(parsed, checkpoint=check)
        else:
            snapshot = snapshot_product_git_delivery_plan_v2(parsed, checkpoint=check)
        if _encode(snapshot, check) != body:
            raise invalid_git_delivery_plan()
        check()
        return cast(T, snapshot)
    except (KernelError, ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        if callback_error is not None:
            raise callback_error from None
        raise invalid_git_delivery_plan() from None
