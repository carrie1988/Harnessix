"""完整父目录历史的路由契约；复用原调用、资源与摘要校验。"""

from __future__ import annotations

from typing import Literal

from harnessix.execution.versioned_contracts import ExecutionPlanV3
from harnessix.trusted_actions.contracts import ActionRoutePlan, ActionRouteSnapshot


class ActionRoutePlanV2(ActionRoutePlan):
    spec_version: Literal["harnessix.action-route-plan/v2"] = "harnessix.action-route-plan/v2"  # type: ignore[assignment]
    execution: ExecutionPlanV3


class ActionRouteSnapshotV2(ActionRouteSnapshot):
    spec_version: Literal["harnessix.action-route-snapshot/v2"] = (
        "harnessix.action-route-snapshot/v2"  # type: ignore[assignment]
    )
    plan: ActionRoutePlanV2
