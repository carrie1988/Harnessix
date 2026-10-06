"""宿主异步准备结果的严格资源快照；不接纳 Policy、执行器或上下文覆盖。"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from pydantic import BaseModel, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.contracts import ActionRouteSnapshot, CanonicalActionResource
from harnessix.trusted_actions.planning import _canonical_resources
from harnessix.trusted_actions.router import ActionPlanningContext, ResolvedAction
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2
from harnessix.workspace.contracts import (
    ExternalRoot,
    WorkspaceResourceObservation,
    WorkspaceResourceRequest,
)
from harnessix.workspace.parent_closure_contracts import WorkspaceParentClosureReference
from harnessix.workspace.snapshot_capture import _prepare_requests
from harnessix.workspace.snapshot_contracts import WorkspaceSnapshotV2

_MODELS = frozenset(
    {
        CanonicalActionResource,
        WorkspaceResourceRequest,
        WorkspaceSnapshotV2,
        ExternalRoot,
        WorkspaceResourceObservation,
        WorkspaceParentClosureReference,
    }
)


def _invalid() -> KernelError:
    return KernelError("action_preparation_invalid", "Action可信准备结果不符合契约")


def _inspect(value: object, checkpoint: Callable[[], None]) -> None:
    """有限模型图不接受子类、额外字段或伪造容器；每个成员消费原检查点。"""
    checkpoint()
    kind = type(value)
    if kind in _MODELS:
        model = cast(BaseModel, value)
        if (
            set(vars(model)) != set(type(model).model_fields)
            or model.__pydantic_extra__ is not None
        ):
            raise _invalid()
        for name in type(model).model_fields:
            _inspect(getattr(model, name), checkpoint)
    elif kind is tuple:
        for item in cast(tuple[object, ...], value):
            _inspect(item, checkpoint)
    elif kind not in {str, int, type(None)}:
        raise _invalid()


def _snapshot[T: BaseModel](value: T, kind: type[T], checkpoint: Callable[[], None]) -> T:
    if type(value) is not kind:
        raise _invalid()
    _inspect(value, checkpoint)
    try:
        return kind.model_validate(value.model_dump(warnings="error"), strict=True)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise _invalid() from None


def snapshot_prepared_resolution(
    value: object, *, checkpoint: Callable[[], None]
) -> ResolvedAction:
    """只重建原资源、请求和 Snapshot2；不把预期观察当真实捕获。"""
    checkpoint()
    if (
        type(value) is not ResolvedAction
        or type(value.resources) is not tuple
        or type(value.workspace_resources) is not tuple
    ):
        raise _invalid()
    resources = tuple(
        _snapshot(item, CanonicalActionResource, checkpoint) for item in value.resources
    )
    requests = tuple(
        _snapshot(item, WorkspaceResourceRequest, checkpoint) for item in value.workspace_resources
    )
    expected = value.expected_workspace
    if expected is not None:
        expected = _snapshot(expected, WorkspaceSnapshotV2, checkpoint)
    checkpoint()
    return ResolvedAction(resources, requests, expected)


def validate_prepared_route_resolution(
    route: ActionRouteSnapshot, prepared: ResolvedAction, context: ActionPlanningContext
) -> None:
    """竞态胜出的原计划须绑定同一全部请求；不以资源摘要取代 Workspace 请求。"""
    workspace = route.plan.execution.workspace
    actual = tuple(
        WorkspaceResourceRequest(location=item.location, path=item.path, access=item.access)
        for item in workspace.resources
    )
    if (
        route.plan.resources != _canonical_resources(prepared.resources)
        or actual
        != _prepare_requests(prepared.workspace_resources, context.cwd, workspace.platform)
        or (
            prepared.expected_workspace is not None
            and (
                not isinstance(route.plan, ActionRoutePlanV2)
                or route.plan.execution.workspace != prepared.expected_workspace
            )
        )
    ):
        raise KernelError("action_invocation_conflict", "Action调用标识已经绑定其他计划")
