"""产品Action启动恢复扫描：核对跨Store引用并保守收敛孤儿Process。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Item,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
)
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import utc_now
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_runtime_types import ProductProcessSupervisor
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.recovery_contracts import (
    ActionRecoveryScanReport,
    ActionRuntimeFence,
    action_recovery_scan_report_digest,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore


@dataclass(frozen=True, slots=True)
class _SessionActionReferences:
    plan_ids: frozenset[UUID]
    review_call_ids: frozenset[UUID]
    output_call_ids: frozenset[UUID]


async def scan_product_action_recovery(
    *,
    plans: SQLiteExecutionPlanStore,
    audit: SQLiteActionAuditStore,
    sessions: SQLiteSessionStore,
    artifacts: SQLiteArtifactStore,
    supervisor: ProductProcessSupervisor | None,
    fence: ActionRuntimeFence,
) -> ActionRecoveryScanReport:
    """扫描Plan/Audit、Session、Process和Artifact；不会重放任何Action效果。"""

    routes = audit.routes()
    repaired = 0
    invalid = 0
    for route in routes:
        plan_id = route.plan.execution.plan_id
        try:
            persisted = plans.load_plan(plan_id)
        except KernelError as error:
            if error.code != "execution_plan_not_found":
                raise
            plans.save_plan(route.plan.execution)
            repaired += 1
        else:
            if persisted != route.plan.execution:
                invalid += 1

    session_refs = await _session_action_references(sessions)
    route_ids = frozenset(route.plan.execution.plan_id for route in routes)
    product_route_ids = frozenset(
        route.plan.execution.plan_id
        for route in routes
        if route.plan.binding.source == "builtin"
        and route.plan.binding.source_id == "harnessix.product"
    )
    session_orphans = len(session_refs.plan_ids - route_ids)
    routes_without_session = len(product_route_ids - session_refs.plan_ids)

    artifact_orphans = 0
    for purpose, call_id in await artifacts.action_recovery_inventory():
        referenced = (
            call_id in session_refs.review_call_ids
            if purpose == "action_review"
            else call_id in session_refs.output_call_ids
        )
        artifact_orphans += not referenced

    process_orphans = 0
    if supervisor is not None:
        process_routes = {
            route.plan.execution.plan_id: route
            for route in routes
            if route.plan.binding.executor_id.startswith("product.process-profile.")
        }
        for lease in supervisor.active_leases():
            process_route = process_routes.get(lease.plan_id)
            if process_route is None or process_route.state not in {
                "running",
                "unknown",
                "reconciling",
            }:
                process_orphans += 1
                await supervisor.reconcile(lease.process_id)

    operations = audit.operations(active_only=True)
    now = utc_now()
    candidate = ActionRecoveryScanReport.model_construct(
        _fields_set=None,
        owner_generation=fence.generation,
        scanned_routes=len(routes),
        repaired_execution_plans=repaired,
        invalid_execution_plans=invalid,
        active_operations=len(operations),
        expired_operations=sum(item.deadline <= now for item in operations),
        process_orphan_leases=process_orphans,
        session_orphan_references=session_orphans,
        routes_without_session_reference=routes_without_session,
        artifact_orphans=artifact_orphans,
        created_at=now,
        report_sha256="0" * 64,
    )
    return ActionRecoveryScanReport(
        **candidate.model_dump(exclude={"report_sha256"}),
        report_sha256=action_recovery_scan_report_digest(candidate),
    )


async def _session_action_references(
    sessions: SQLiteSessionStore,
) -> _SessionActionReferences:
    plan_ids: set[UUID] = set()
    review_calls: set[UUID] = set()
    output_calls: set[UUID] = set()
    for thread_id in await sessions.thread_ids():
        thread = await sessions.get_thread(thread_id)
        collections: list[Iterable[Item]] = [turn.items for turn in thread.turns]
        if thread.fork_snapshot is not None:
            collections.append(thread.fork_snapshot.items)
        for items in collections:
            for item in items:
                content = item.content
                if isinstance(content, TrustedActionApprovalRequestContent):
                    plan_ids.add(content.plan_id)
                    review_calls.add(content.call_id)
                elif isinstance(content, ToolResultContent) and content.trusted_action is not None:
                    plan_ids.add(content.trusted_action.plan_id)
                    output_calls.add(content.call_id)
    return _SessionActionReferences(
        plan_ids=frozenset(plan_ids),
        review_call_ids=frozenset(review_calls),
        output_call_ids=frozenset(output_calls),
    )
