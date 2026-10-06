"""Core2/Plan2 显式规范字节入口；复用原唯一 512KiB 编解码算法。"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.product_config.git_delivery_observed_contracts import (
    ProductGitDeliveryCoreV2,
    ProductGitDeliveryPlanV2,
)
from harnessix.product_config.git_delivery_plan_snapshot import (
    snapshot_product_git_delivery_core_v2 as snapshot_product_git_delivery_core_v2,
)
from harnessix.product_config.git_delivery_plan_snapshot import (
    snapshot_product_git_delivery_plan_v2 as snapshot_product_git_delivery_plan_v2,
)
from harnessix.product_config.git_delivery_plan_wire import _decode, _encode


def encode_product_git_delivery_core_v2(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """全用户观察参与原内容地址，只排除Core自身指纹。"""
    return _encode(snapshot_product_git_delivery_core_v2(value, checkpoint=checkpoint), checkpoint)


def decode_product_git_delivery_core_v2(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryCoreV2:
    """只接纳Core2规范完整正文，旧代际或缺失观察均拒绝。"""
    return _decode(body, ProductGitDeliveryCoreV2, checkpoint)


def encode_product_git_delivery_plan_v2(value: object, *, checkpoint: Callable[[], None]) -> bytes:
    """完整Plan2包含嵌套全部指纹和原封套指纹，不截断或升级限额。"""
    return _encode(snapshot_product_git_delivery_plan_v2(value, checkpoint=checkpoint), checkpoint)


def decode_product_git_delivery_plan_v2(
    body: object, *, checkpoint: Callable[[], None]
) -> ProductGitDeliveryPlanV2:
    """与Core2同一严格JSON算法；不接纳Plan1或规范同义字节。"""
    return _decode(body, ProductGitDeliveryPlanV2, checkpoint)
