"""Git业务快照与原Session、Route、Transaction、Artifact和CAS的只读交叉验真。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import aiosqlite
from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.artifacts.persistence import ARTIFACT_READ_SELECT
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.artifacts.reference import validate_artifact_reference
from harnessix.artifacts.sqlite import records
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.git_approval_history_projection import (
    interpret_git_approval_history,
)
from harnessix.product_config.git_approval_history_proof import ApprovalHistoryEvidence
from harnessix.product_config.git_business_snapshot_contracts import (
    GitDeliveryBusinessSnapshot,
)
from harnessix.product_config.git_decision_link_sources import (
    build_git_decision_link_sources,
)
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_materials import (
    read_product_git_delivery_core_materials_v2,
)
from harnessix.product_config.git_delivery_review_codec import (
    build_product_git_action_review,
    encode_product_git_action_review,
)
from harnessix.product_config.git_delivery_route_core import (
    load_product_git_delivery_route_core_v2,
)
from harnessix.product_config.git_delivery_source import (
    verify_persisted_git_delivery_source,
)
from harnessix.product_config.git_prepared_link_proof import PreparedLinkEvidence
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.session.capacity import load_thread_facts
from harnessix.session.event_body_refs import EventBodyRef
from harnessix.session.maintenance_io import MaintenanceIOControl
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.session.sqlite_publication import authenticated_events
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.trusted_actions.versioned_contracts import ActionRouteSnapshotV2


def _invalid() -> KernelError:
    return KernelError(
        "git_business_association_invalid", "Git业务快照与原产品事实不一致"
    )


class _VerificationOnlyScope:
    """只允许原Artifact证明验真，不暴露新的发布能力。"""

    def publication_context(self) -> dict[str, object]:
        return {"scope_id": str(UUID(int=0)), "bindings": []}

    def assert_public_json(self, value: JsonValue, *, checkpoint: Callable[[], None]) -> None:
        raise _invalid()

    def assert_public_jsonl(self, body: bytes, *, checkpoint: Callable[[], None]) -> None:
        raise _invalid()


class _Control:
    """记住原检查点异常，避免SQLite中断或业务映射覆盖其身份。"""

    def __init__(self, control: MaintenanceIOControl) -> None:
        self._control = control
        self.error: BaseException | None = None

    def checkpoint(self) -> None:
        try:
            self._control.checkpoint()
        except BaseException as error:
            self.error = error
            raise

    def interrupt(self) -> int:
        try:
            self.checkpoint()
        except BaseException:
            return 1
        return 0


@dataclass(frozen=True, slots=True)
class _SessionEvidence:
    histories: dict[UUID, AuthenticatedThreadHistory]
    review_bodies: dict[UUID, bytes]


async def _read_session_evidence(
    tree: PrivateStateTree,
    snapshot: GitDeliveryBusinessSnapshot,
    binding: SessionPublicationBinding,
    control: _Control,
) -> _SessionEvidence:
    """在同一只读事务内认证所需Thread全历史及其精确Review正文。"""
    database = await aiosqlite.connect((tree.path / "sessions.db").as_uri() + "?mode=ro", uri=True)
    try:
        database.row_factory = aiosqlite.Row
        await database.set_progress_handler(control.interrupt, 1000)
        await database.execute("PRAGMA query_only=ON")
        await database.execute("BEGIN")
        facts = await load_thread_facts(database, binding)
        threads = {fact.thread.thread_id: fact.thread for fact in facts}
        required = {link.prepared.plan.core.thread_id for link in snapshot.links}
        if not required <= threads.keys():
            raise _invalid()

        histories: dict[UUID, AuthenticatedThreadHistory] = {}
        for thread_id in sorted(required, key=lambda value: value.hex):
            control.checkpoint()
            refs: list[EventBodyRef] = []
            events = await authenticated_events(
                database,
                binding,
                thread_id,
                0,
                history_checkpoint=control.checkpoint,
                _body_refs=refs,
            )
            histories[thread_id] = AuthenticatedThreadHistory(
                threads[thread_id], tuple(events), tuple(refs)
            )

        guard = ArtifactPublicationGuard(_VerificationOnlyScope(), binding)
        review_bodies: dict[UUID, bytes] = {}
        for link in snapshot.links:
            control.checkpoint()
            core = link.prepared.plan.core
            expected = link.prepared.plan.review_artifact
            cursor = await database.execute(
                ARTIFACT_READ_SELECT + " WHERE artifact_id = ?", (str(expected.artifact_id),)
            )
            row = await cursor.fetchone()
            if row is None:
                raise _invalid()
            actual = validate_artifact_reference(row, histories[core.thread_id].thread)
            guard.require_proof(row)
            body = row["body"]
            if (
                actual != expected
                or row["state"] != "published"
                or row["purpose"] != "action_review"
                or row["thread_id"] != str(core.thread_id)
                or row["turn_id"] != str(core.turn_id)
                or row["call_id"] != str(core.call.call_id)
                or type(body) is not bytes
                or hashlib.sha256(body).hexdigest() != expected.sha256
                or len(body) != expected.size_bytes
                or len(records(body)) != expected.records
                or review_bodies.setdefault(expected.artifact_id, body) != body
            ):
                raise _invalid()
        control.checkpoint()
        return _SessionEvidence(histories, review_bodies)
    finally:
        await database.close()


def _read_blob(
    store: SQLiteWorkspaceTransactionStore, checkpoint: Callable[[], None]
) -> Callable[[str], bytes]:
    def read(digest: str) -> bytes:
        checkpoint()
        body = store.blob(digest, checkpoint=checkpoint)
        checkpoint()
        return body

    return read


def _verify_links(
    tree: PrivateStateTree,
    snapshot: GitDeliveryBusinessSnapshot,
    binding: SessionPublicationBinding,
    evidence: _SessionEvidence,
    control: _Control,
) -> None:
    """逐关联复用原Reader和原唯一算法，不从快照摘要补造事实。"""
    with (
        SQLiteWorkspaceTransactionStore(
            tree.path / "workspace-transactions",
            read_only=True,
            checkpoint=control.checkpoint,
        ) as transactions,
    ):
        read_blob = _read_blob(transactions, control.checkpoint)
        with (
            SQLiteExecutionPlanStore(
                tree.path / "execution-plans.db",
                read_only=True,
                read_blob=read_blob,
                checkpoint=control.checkpoint,
            ) as plans,
            SQLiteActionAuditStore(
                tree.path / "action-audit.db",
                read_only=True,
                read_blob=read_blob,
                checkpoint=control.checkpoint,
            ) as audit,
        ):
            router = TrustedActionRouter(
                plans=plans,
                audit=audit,
                workspace_root=lambda _: Path("/"),
            )
            cores = ProductGitDeliveryCoreStore(transactions)
            cas = GitMaterialCAS(transactions)
            identity = binding.git_verifier.identity()
            for item in snapshot.links:
                control.checkpoint()
                prepared, decision = item.prepared, item.decision
                core, route_plan = prepared.plan.core, prepared.plan.route
                route_id = route_plan.execution.plan_id
                history = evidence.histories[core.thread_id]
                route = router.status(route_id, checkpoint=control.checkpoint)
                if (
                    type(route) is not ActionRouteSnapshotV2
                    or (core.store_id, core.key_id) != identity
                    or route.plan != route_plan
                    or plans.load_plan(route_id) != route_plan.execution
                    or load_product_git_delivery_route_core_v2(
                        cores, route_plan, checkpoint=control.checkpoint
                    )
                    != core
                ):
                    raise _invalid()

                verify_persisted_git_delivery_source(
                    history.thread,
                    core.baseline.source,
                    router,
                    transactions,
                    checkpoint=control.checkpoint,
                )
                materials = read_product_git_delivery_core_materials_v2(
                    cas, core, checkpoint=control.checkpoint
                )
                review = encode_product_git_action_review(
                    build_product_git_action_review(
                        core, materials.diff, checkpoint=control.checkpoint
                    ),
                    checkpoint=control.checkpoint,
                )
                if evidence.review_bodies[prepared.plan.review_artifact.artifact_id] != review:
                    raise _invalid()

                route_events = router.events(route_id)
                approval = router.approval(route_id)
                projection = interpret_git_approval_history(
                    history,
                    prepared,
                    route,
                    route_events,
                    approval,
                    checkpoint=control.checkpoint,
                )
                if decision is None:
                    if projection.state != "pending" or approval is not None:
                        raise _invalid()
                else:
                    rebuilt = build_git_decision_link_sources(
                        ApprovalHistoryEvidence(
                            PreparedLinkEvidence(prepared, history, route, review),
                            projection,
                            route_events,
                            approval,
                            None,
                        ),
                        checkpoint=control.checkpoint,
                    )
                    if rebuilt != decision:
                        raise _invalid()
            control.checkpoint()


def validate_git_business_associations(
    tree: PrivateStateTree,
    snapshot: GitDeliveryBusinessSnapshot,
    binding: SessionPublicationBinding,
    control: MaintenanceIOControl,
) -> None:
    """核对快照的全部当前业务关联；成功不授予恢复后执行权。"""
    guarded = _Control(control)
    try:
        guarded.checkpoint()
        evidence = asyncio.run(_read_session_evidence(tree, snapshot, binding, guarded))
        _verify_links(tree, snapshot, binding, evidence, guarded)
        guarded.checkpoint()
    except BaseException as error:
        if guarded.error is not None:
            raise guarded.error from None
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise _invalid() from None
