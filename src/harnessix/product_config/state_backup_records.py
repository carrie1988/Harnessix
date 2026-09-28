"""完整备份的跨Store只读核验；复用正式Reader，不装配Router、Supervisor或Executor。"""

from __future__ import annotations

import math
import re
from contextlib import closing
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread, ToolResultContent, TrustedActionApprovalRequestContent
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES, WorkspaceTransactionRecord
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import utc_now
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.owner_receipt import (
    MAX_OWNER_RECEIPT_BYTES,
    ProcessOwnerReceipt,
    verify_owner_receipt,
)
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervision_store import SQLiteProcessLeaseStore
from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.state_backup_contracts import PROCESS_DATABASE
from harnessix.product_config.state_backup_files import PrivateStateTree, file_digest, read_small
from harnessix.session.maintenance_io import MaintenanceIOControl
from harnessix.sqlite_readonly import readonly_database
from harnessix.trusted_actions.store import SQLiteActionAuditStore


def _invalid() -> KernelError:
    return KernelError("product_backup_state_invalid", "产品备份Schema、原来源或跨Store事实不完整")


def _config(tree: PrivateStateTree, control: MaintenanceIOControl) -> None:
    with SQLiteProductRuntimeConfigStore(tree.path / "product-config.db", read_only=True) as store:
        store._db.set_progress_handler(control.interrupt, 1000)
        for (digest,) in store._db.execute("SELECT config_sha256 FROM product_config_snapshots"):
            control.checkpoint()
            store.load_snapshot(digest)
        for (digest,) in store._db.execute(
            "SELECT config_sha256 FROM product_action_config_snapshots"
        ):
            control.checkpoint()
            store.load_action_snapshot(digest)
        store.active()
        store.active_action()
        store.config_events()
        store.action_config_events()
        store.fallback_events()
        store.action_recovery_reports()
        store.action_recovery_scans()


def _plans_and_routes(
    tree: PrivateStateTree, threads: tuple[Thread, ...], control: MaintenanceIOControl
) -> None:
    with (
        SQLiteExecutionPlanStore(tree.path / "execution-plans.db", read_only=True) as plans,
        SQLiteActionAuditStore(tree.path / "action-audit.db", read_only=True) as audit,
    ):
        plans._db.set_progress_handler(control.interrupt, 1000)
        audit._db.set_progress_handler(control.interrupt, 1000)
        for row in plans._db.execute(
            "SELECT plan_id,fingerprint,workspace_id,workspace_revision FROM execution_plans"
        ):
            control.checkpoint()
            plan = plans.load_plan(UUID(row[0]))
            if row != (
                str(plan.plan_id),
                plan.fingerprint,
                plan.workspace.workspace_id,
                plan.workspace.revision,
            ):
                raise _invalid()
            approval = plans.load_approval(plan.plan_id)
            if approval is not None and approval.plan_fingerprint != plan.fingerprint:
                raise _invalid()
        routes = audit.routes()
        route_ids = {route.plan.execution.plan_id for route in routes}
        for route in routes:
            control.checkpoint()
            audit.events(route.plan.execution.plan_id)
            if plans.load_plan(route.plan.execution.plan_id) != route.plan.execution:
                raise _invalid()
        audit.operations()
        referenced = _thread_route_ids(threads)
        product_routes = {
            route.plan.execution.plan_id
            for route in routes
            if route.plan.binding.source == "builtin"
            and route.plan.binding.source_id == "harnessix.product"
        }
        if not referenced <= route_ids or not product_routes <= referenced:
            raise _invalid()


def _thread_route_ids(threads: tuple[Thread, ...]) -> set[UUID]:
    """包含归档Thread和Fork继承项目，不仅检查当前活动Turn。"""
    referenced = set()
    for thread in threads:
        collections = [turn.items for turn in thread.turns]
        if thread.fork_snapshot is not None:
            collections.append(thread.fork_snapshot.items)
        for items in collections:
            for item in items:
                content = item.content
                if isinstance(content, TrustedActionApprovalRequestContent):
                    referenced.add(content.plan_id)
                elif isinstance(content, ToolResultContent) and content.trusted_action is not None:
                    referenced.add(content.trusted_action.plan_id)
    return referenced


def _delivery(
    tree: PrivateStateTree, paths: tuple[str, ...], control: MaintenanceIOControl
) -> None:
    for path in paths:
        if path.startswith("workspace-transactions/blobs/"):
            size, digest = file_digest(tree, path, control)
            if size > MAX_TRANSACTION_FILE_BYTES or digest != path.rsplit("/", 1)[1]:
                raise _invalid()
    with SQLiteWorkspaceTransactionStore(
        tree.path / "workspace-transactions", read_only=True
    ) as store:
        store._db.set_progress_handler(control.interrupt, 1000)
        for (identity,) in store._db.execute("SELECT transaction_id FROM workspace_transactions"):
            control.checkpoint()
            record = store.load(UUID(identity))
            rows = store._db.execute(
                "SELECT sequence,state,payload FROM workspace_transaction_events "
                "WHERE transaction_id=? ORDER BY sequence",
                (identity,),
            )
            for index, (sequence, state, payload) in enumerate(rows):
                control.checkpoint()
                event = WorkspaceTransactionRecord.model_validate_json(payload)
                if (
                    sequence != index
                    or event.sequence != index
                    or event.state != state
                    or event.plan != record.plan
                    or event.transaction_id != record.transaction_id
                ):
                    raise _invalid()
            for mutation in record.plan.mutations:
                for version in (mutation.before, mutation.after):
                    if version.presence == "file":
                        assert version.sha256 is not None
                        path = "workspace-transactions/blobs/" + version.sha256
                        if path not in paths or file_digest(tree, path, control) != (
                            version.size,
                            version.sha256,
                        ):
                            raise _invalid()


def _leases(tree: PrivateStateTree, control: MaintenanceIOControl) -> None:
    with closing(readonly_database(tree.path / "workspace-leases.db")) as database:
        database.set_progress_handler(control.interrupt, 1000)
        for workspace, owner, token, expiry, updated in database.execute(
            "SELECT * FROM workspace_leases"
        ):
            control.checkpoint()
            if (
                type(workspace) is not str
                or re.fullmatch(r"[0-9a-f]{64}", workspace) is None
                or type(token) is not int
                or token < 1
                or not math.isfinite(expiry)
                or not math.isfinite(updated)
                or (owner is not None and (type(owner) is not str or not 1 <= len(owner) <= 128))
            ):
                raise _invalid()
            if owner is not None and expiry > utc_now().timestamp():
                raise KernelError("product_backup_not_quiet", "产品状态仍有活跃Workspace租约")


def _processes(
    tree: PrivateStateTree, paths: tuple[str, ...], control: MaintenanceIOControl
) -> None:
    if PROCESS_DATABASE not in paths:
        return
    with (
        SQLiteProcessLeaseStore(tree.path / PROCESS_DATABASE, read_only=True) as store,
        SQLiteExecutionPlanStore(tree.path / "execution-plans.db", read_only=True) as plans,
    ):
        store._db.set_progress_handler(control.interrupt, 1000)
        plans._db.set_progress_handler(control.interrupt, 1000)
        if store.active():
            raise KernelError("product_backup_not_quiet", "产品状态仍有活跃Process租约")
        leases = {}
        for (identity,) in store._db.execute("SELECT process_id FROM process_leases"):
            control.checkpoint()
            lease = store.load(UUID(identity))
            if plans.load_plan(lease.plan_id).fingerprint != lease.plan_fingerprint:
                raise _invalid()
            leases[identity] = lease
            rows = store._db.execute(
                "SELECT sequence,state,payload FROM process_lease_events "
                "WHERE process_id=? ORDER BY sequence",
                (identity,),
            )
            for index, (sequence, state, payload) in enumerate(rows):
                event = ProcessLease.model_validate_json(payload)
                if (
                    sequence != index
                    or event.sequence != index
                    or event.state != state
                    or str(event.process_id) != identity
                ):
                    raise _invalid()
        _process_files(tree, paths, leases, control)
        for identity, lease in leases.items():
            if lease.owner_identity is not None:
                prefix = "process-owner/runs/" + identity + "/"
                if not {
                    prefix + name for name in ("receipt.json", "stdout.bin", "stderr.bin")
                } <= set(paths):
                    raise _invalid()


def _process_files(
    tree: PrivateStateTree,
    paths: tuple[str, ...],
    leases: dict[str, ProcessLease],
    control: MaintenanceIOControl,
) -> None:
    """仅验原Receipt MAC和原输出；不查PID、不协调Owner、不杀进程。"""
    for path in paths:
        if not path.startswith("process-owner/runs/"):
            continue
        identity = path.split("/")[2]
        linked_lease = leases.get(identity)
        if linked_lease is None:
            raise _invalid()
        if path.endswith("receipt.json"):
            receipt = ProcessOwnerReceipt.model_validate_json(
                read_small(tree, path, MAX_OWNER_RECEIPT_BYTES)
            )
            verify_owner_receipt(
                receipt,
                owner_token=linked_lease.owner_token,
                process_id=linked_lease.process_id,
                owner_identity=linked_lease.owner_identity,
            )
            if receipt.state == "running":
                raise KernelError("product_backup_not_quiet", "Process原回执仍为运行中")
        else:
            observation = (
                linked_lease.stdout if path.endswith("stdout.bin") else linked_lease.stderr
            )
            if file_digest(tree, path, control) != (
                observation.persisted_bytes,
                observation.persisted_sha256,
            ):
                raise _invalid()


def validate_state_records(
    tree: PrivateStateTree,
    threads: tuple[Thread, ...],
    paths: tuple[str, ...],
    control: MaintenanceIOControl,
) -> None:
    _config(tree, control)
    _plans_and_routes(tree, threads, control)
    _delivery(tree, paths, control)
    _leases(tree, control)
    _processes(tree, paths, control)
