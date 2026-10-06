"""从原 Router 的唯一完整资源读回 Core；声明一致性不等于归属或批准。

调用方必须先经原 Router 的认证读取边界取得 Route。本模块拒绝伪造模型外形，
按完整资源中的 Core 内容地址读原 CAS，再复用封套的全部交叉字段校验。
它不读取 Session、验证 MAC、发布 Review 或写入审批；纯摘要不能补出这些事实。
"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_contracts import (
    ProductGitDeliveryCore,
    validate_product_git_delivery_route,
)
from harnessix.product_config.git_delivery_plan_snapshot import (
    _snapshot,
    invalid_git_delivery_plan,
)
from harnessix.trusted_actions.planning import external_action_identity
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2


def load_product_git_delivery_route_core(
    core_store: ProductGitDeliveryCoreStore,
    route: ActionRoutePlanV2,
    *,
    checkpoint: Callable[[], None],
) -> ProductGitDeliveryCore:
    """严格 Route2 → 唯一写资源 → 原完整 CAS Core → 同一原交叉字段校验。

    保留原取消、期限和 CAS 错误；拒绝条件只公开固定元数据，不携带正文。
    原 Router 的外部身份必须由稳定 invocation 与完整 binding 摘要派生，不能
    以任意自报 Delivery UUID 同时填入 Core 和 Route 来替代真实规划身份。
    """
    checked = _snapshot(route, ActionRoutePlanV2, checkpoint)
    checkpoint()
    if (
        type(core_store) is not ProductGitDeliveryCoreStore
        or len(checked.resources) != 1
        or (checked.resources[0].kind, checked.resources[0].access) != ("external", "write")
        or checked.external_action_id
        != external_action_identity(checked.invocation, checked.binding)
    ):
        raise invalid_git_delivery_plan()
    core = core_store.load(checked.resources[0].attributes_sha256, checkpoint=checkpoint)
    checkpoint()
    try:
        validate_product_git_delivery_route(core, checked)
    except ValueError:
        raise invalid_git_delivery_plan() from None
    checkpoint()
    return core
