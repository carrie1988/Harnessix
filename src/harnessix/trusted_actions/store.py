"""以SQLite保存Action路由快照和连续Hash链审计，不持有Executor或Secret。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.sqlite_readonly import readonly_database
from harnessix.trusted_actions.contracts import (
    ActionAuditEvent,
    ActionRoutePlan,
    ActionRouteSnapshot,
    ActionRouteState,
    build_audit_event,
)
from harnessix.trusted_actions.operation_store import (
    ActionOperationStoreMixin,
    initialize_action_operation_schema,
)
from harnessix.trusted_actions.ownership_store import (
    ActionOwnershipStoreMixin,
    initialize_action_owner_schema,
)
from harnessix.trusted_actions.transition_store import ActionTransitionStoreMixin
from harnessix.trusted_actions.versioned_contracts import ActionRoutePlanV2, ActionRouteSnapshotV2
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.parent_closure_codec import read_workspace_parent_closure

_SCHEMA_VERSION = "2"
_PARENT_CLOSURE_SCHEMA_VERSION = "3"
ActionRoutePlanAny = ActionRoutePlan | ActionRoutePlanV2
ActionRouteSnapshotAny = ActionRouteSnapshot | ActionRouteSnapshotV2
_PLAN_ADAPTER: TypeAdapter[ActionRoutePlanAny] = TypeAdapter(
    Annotated[ActionRoutePlanAny, Field(discriminator="spec_version")]
)


def _check_read_checkpoint(checkpoint: Callable[[], None] | None) -> None:
    """单次Reader入口检查；无参数旧调用保持无额外控制。"""
    if checkpoint is not None:
        checkpoint()


def _decode_plan(payload: str) -> ActionRoutePlanAny:
    value = json.loads(payload)
    if isinstance(value, dict) and "spec_version" not in value:
        # 原Route1默认值只在旧模型内生效，不修改正文或接纳无标签Route2。
        return ActionRoutePlan.model_validate_json(payload, strict=True)
    return _PLAN_ADAPTER.validate_json(payload, strict=True)


def _validate_plan(plan: ActionRoutePlanAny) -> ActionRoutePlanAny:
    try:
        return _decode_plan(plan.model_dump_json(warnings="error"))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise KernelError("action_route_plan_invalid", "Action Route Plan不符合契约") from None


class _ActionAuditSchema:
    """仅管理审计存储代际，保留原 Owner 一到二代迁移。"""

    _db: sqlite3.Connection

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS action_audit_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM action_audit_metadata WHERE key = 'schema_version'"
        ).fetchone()
        previous_version: str | None = None
        if row is None:
            self._db.execute(
                "INSERT INTO action_audit_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] in {"1", _SCHEMA_VERSION, _PARENT_CLOSURE_SCHEMA_VERSION}:
            previous_version = row[0]
        else:
            raise KernelError("action_audit_store_version", "Action审计存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_route_plans (
                plan_id TEXT PRIMARY KEY,
                invocation_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS action_route_snapshots (
                plan_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                last_event_digest TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES action_route_plans(plan_id)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS action_audit_events (
                plan_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(plan_id, sequence),
                UNIQUE(digest),
                FOREIGN KEY(plan_id) REFERENCES action_route_plans(plan_id)
            ) STRICT;
            """
        )
        initialize_action_owner_schema(self._db)
        initialize_action_operation_schema(self._db)
        if previous_version == "1":
            self._db.execute(
                "UPDATE action_audit_metadata SET value = ? WHERE key = 'schema_version'",
                (_SCHEMA_VERSION,),
            )

    def _check_schema(self) -> None:
        row = self._db.execute(
            "SELECT value FROM action_audit_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row[0] not in {"1", _SCHEMA_VERSION, _PARENT_CLOSURE_SCHEMA_VERSION}:
            raise KernelError("action_audit_store_version", "Action审计存储版本不受支持")


class _ActionRouteClosureReader:
    """仅完整读取路由父历史，内部坏数据和上游控制异常分别处理。"""

    _read_blob: Callable[[str], bytes] | None
    _checkpoint: Callable[[], None]

    def _validate_parent_closure(
        self,
        plan: ActionRoutePlanAny,
        *,
        error_code: str,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(plan, ActionRoutePlanV2):
            return
        if self._read_blob is None:
            raise KernelError(error_code, "Action Route Plan缺少完整父目录历史读取端口")

        def check() -> None:
            try:
                self._checkpoint()
                if checkpoint is not None:
                    checkpoint()
            except BaseException as error:
                # 上游控制不能因错误码恰与历史损坏相同而被重分类。
                raise UpstreamCheckpointError(error) from None

        try:
            read_workspace_parent_closure(
                plan.execution.workspace, self._read_blob, checkpoint=check
            )
        except UpstreamCheckpointError as error:
            raise error.error from None
        except KernelError as error:
            if error.code != "workspace_closure_corrupt":
                raise
            raise KernelError(error_code, "Action Route Plan父目录历史损坏") from None


class _ActionAuditHistoryReader:
    """核对全部审计事件与当前计划绑定及连续 Hash 链。"""

    _db: sqlite3.Connection

    if TYPE_CHECKING:

        def load(self, plan_id: UUID) -> ActionRouteSnapshotAny: ...

    def events(self, plan_id: UUID) -> tuple[ActionAuditEvent, ...]:
        snapshot = self.load(plan_id)
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM action_audit_events "
            "WHERE plan_id = ? ORDER BY sequence",
            (str(plan_id),),
        ).fetchall()
        try:
            events = tuple(ActionAuditEvent.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("action_audit_store_corrupt", "Action审计事件损坏") from None
        previous: str | None = None
        state: str | None = None
        for index, (row, event) in enumerate(zip(rows, events, strict=True), start=1):
            if (
                row[0] != index
                or row[1] != event.digest
                or event.plan_id != plan_id
                or event.plan_fingerprint != snapshot.plan.fingerprint
                or event.resource_sha256 != snapshot.plan.resources_sha256
                or event.policy_id != snapshot.plan.execution.policy.policy_id
                or event.policy_version != snapshot.plan.execution.policy.version
                or event.sequence != index
                or event.previous_digest != previous
                or event.from_state != state
            ):
                raise KernelError("action_audit_store_corrupt", "Action审计事件链损坏")
            previous = event.digest
            state = event.to_state
        if (
            len(events) != snapshot.sequence
            or previous != snapshot.last_event_digest
            or state != snapshot.state
        ):
            raise KernelError("action_audit_store_corrupt", "Action审计事件链不完整")
        return events


class SQLiteActionAuditStore(
    _ActionAuditSchema,
    _ActionRouteClosureReader,
    _ActionAuditHistoryReader,
    ActionTransitionStoreMixin,
    ActionOperationStoreMixin,
    ActionOwnershipStoreMixin,
):
    """不可变Route Plan、当前投影和append-only审计事件。"""

    def __init__(
        self,
        path: str | Path,
        *,
        require_runtime_owner: bool = False,
        read_only: bool = False,
        read_blob: Callable[[str], bytes] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self._path = Path(path)
        self._closed = False
        self._require_runtime_owner = require_runtime_owner
        self._runtime_fence = None
        self._read_blob = read_blob
        self._checkpoint = checkpoint if checkpoint is not None else lambda: None
        if read_only:
            self._db = readonly_database(self._path)
            try:
                self._check_schema()
            except BaseException:
                self.close()
                raise
            return
        _prepare_parent(self._path)
        self._db = sqlite3.connect(self._path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if os.name == "posix":
                self._path.chmod(0o600)
            self._initialize()
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def save_plan(
        self, plan: ActionRoutePlanAny, *, initial_state: ActionRouteState
    ) -> ActionRouteSnapshotAny:
        if initial_state not in {"denied", "pending_approval", "ready"}:
            raise KernelError("action_route_state_invalid", "Action初始状态无效")
        checked = _validate_plan(plan)
        self._validate_parent_closure(checked, error_code="action_route_plan_invalid")
        payload = checked.model_dump_json(warnings="error")
        now = utc_now()
        event = build_audit_event(
            checked,
            sequence=1,
            from_state=None,
            to_state=initial_state,
            previous_digest=None,
            error_code=(
                checked.execution.policy.reason_code if initial_state == "denied" else None
            ),
            occurred_at=now,
        )
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._assert_runtime_owner()
            row = self._db.execute(
                "SELECT invocation_id, fingerprint, payload FROM action_route_plans "
                "WHERE plan_id = ?",
                (str(checked.execution.plan_id),),
            ).fetchone()
            expected = (str(checked.invocation.invocation_id), checked.fingerprint, payload)
            if row is None:
                collision = self._db.execute(
                    "SELECT plan_id FROM action_route_plans WHERE invocation_id = ?",
                    (str(checked.invocation.invocation_id),),
                ).fetchone()
                if collision is not None:
                    raise KernelError(
                        "action_invocation_conflict", "Action调用标识已经绑定其他计划"
                    )
                self._db.execute(
                    "INSERT INTO action_route_plans VALUES (?, ?, ?, ?)",
                    (str(checked.execution.plan_id), *expected),
                )
                self._db.execute(
                    "INSERT INTO action_route_snapshots VALUES (?, ?, ?, ?, ?)",
                    (
                        str(checked.execution.plan_id),
                        initial_state,
                        1,
                        event.digest,
                        now.isoformat(),
                    ),
                )
                self._db.execute(
                    "INSERT INTO action_audit_events VALUES (?, ?, ?, ?)",
                    (
                        str(checked.execution.plan_id),
                        event.sequence,
                        event.digest,
                        event.model_dump_json(warnings="error"),
                    ),
                )
            elif row != expected:
                raise KernelError("action_route_plan_conflict", "Action Route Plan内容冲突")
            if isinstance(checked, ActionRoutePlanV2):
                self._db.execute(
                    "UPDATE action_audit_metadata SET value = ? "
                    "WHERE key = 'schema_version' AND value = '2'",
                    (_PARENT_CLOSURE_SCHEMA_VERSION,),
                )
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        return self.load(checked.execution.plan_id)

    def load(
        self, plan_id: UUID, *, checkpoint: Callable[[], None] | None = None
    ) -> ActionRouteSnapshotAny:
        _check_read_checkpoint(checkpoint)
        plan_row = self._db.execute(
            "SELECT invocation_id, fingerprint, payload FROM action_route_plans WHERE plan_id = ?",
            (str(plan_id),),
        ).fetchone()
        snapshot_row = self._db.execute(
            "SELECT state, sequence, last_event_digest, updated_at "
            "FROM action_route_snapshots WHERE plan_id = ?",
            (str(plan_id),),
        ).fetchone()
        if plan_row is None or snapshot_row is None:
            raise KernelError("action_route_not_found", "Action Route Plan不存在")
        try:
            plan = _decode_plan(plan_row[2])
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise KernelError("action_audit_store_corrupt", "Action审计记录损坏") from None
        self._validate_parent_closure(
            plan, error_code="action_audit_store_corrupt", checkpoint=checkpoint
        )
        try:
            event_row = self._db.execute(
                "SELECT digest, payload FROM action_audit_events "
                "WHERE plan_id = ? AND sequence = ?",
                (str(plan_id), snapshot_row[1]),
            ).fetchone()
            if event_row is None:
                raise ValueError
            event = ActionAuditEvent.model_validate_json(event_row[1])
            snapshot: ActionRouteSnapshotAny
            if isinstance(plan, ActionRoutePlanV2):
                snapshot = ActionRouteSnapshotV2(
                    plan=plan,
                    state=snapshot_row[0],
                    sequence=snapshot_row[1],
                    last_event_digest=snapshot_row[2],
                    updated_at=datetime.fromisoformat(snapshot_row[3]),
                )
            else:
                snapshot = ActionRouteSnapshot(
                    plan=plan,
                    state=snapshot_row[0],
                    sequence=snapshot_row[1],
                    last_event_digest=snapshot_row[2],
                    updated_at=datetime.fromisoformat(snapshot_row[3]),
                )
        except (ValidationError, ValueError, TypeError):
            raise KernelError("action_audit_store_corrupt", "Action审计记录损坏") from None
        try:
            indexed_invocation_id = UUID(plan_row[0])
        except (TypeError, ValueError):
            raise KernelError("action_audit_store_corrupt", "Action审计索引损坏") from None
        if (
            plan.execution.plan_id != plan_id
            or plan.invocation.invocation_id != indexed_invocation_id
            or plan.fingerprint != plan_row[1]
            or event.plan_id != plan_id
            or event.plan_fingerprint != plan.fingerprint
            or event.resource_sha256 != plan.resources_sha256
            or event.policy_id != plan.execution.policy.policy_id
            or event.policy_version != plan.execution.policy.version
            or event.sequence != snapshot.sequence
            or event.to_state != snapshot.state
            or event.digest != snapshot.last_event_digest
            or event.digest != event_row[0]
        ):
            raise KernelError("action_audit_store_corrupt", "Action审计索引与事件不一致")
        return snapshot

    def active(self) -> tuple[ActionRouteSnapshotAny, ...]:
        rows = self._db.execute(
            "SELECT plan_id FROM action_route_snapshots "
            "WHERE state IN ('pending_approval', 'ready', 'running', 'unknown', 'reconciling') "
            "ORDER BY plan_id"
        ).fetchall()
        return tuple(self.load(UUID(row[0])) for row in rows)

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteActionAuditStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _prepare_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
