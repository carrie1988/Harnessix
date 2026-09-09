from __future__ import annotations

import os
import sqlite3
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.hooks.contracts import (
    HookDecisionKind,
    HookRegistrySnapshot,
    HookRunEvent,
    HookRunPlan,
    HookRunSnapshot,
    HookRunState,
    build_hook_run_event,
)

_SCHEMA_VERSION = "1"
_ALLOWED_TRANSITIONS: dict[HookRunState, frozenset[HookRunState]] = {
    "ready": frozenset({"running", "failed"}),
    "running": frozenset({"succeeded", "failed", "blocked", "cancelled", "interrupted"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "blocked": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}


class SQLiteHookStore:
    """保存Hook Registry、Run计划、当前投影和Hash链事件。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._closed = False
        self._prepare_parent()
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

    def _prepare_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self._path.parent.chmod(0o700)

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS hook_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM hook_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO hook_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("hook_store_version", "Hook存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hook_registries (
                registry_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(registry_id, generation)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS hook_run_plans (
                run_id TEXT PRIMARY KEY,
                plan_digest TEXT NOT NULL,
                registry_id TEXT NOT NULL,
                registry_generation INTEGER NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS hook_run_snapshots (
                run_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                last_event_digest TEXT NOT NULL,
                decision TEXT,
                output_digest TEXT,
                error_code TEXT,
                updated_at TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS hook_run_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence),
                UNIQUE(digest)
            ) STRICT;
            """
        )

    def next_registry_generation(self, registry_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM hook_registries WHERE registry_id = ?",
            (registry_id,),
        ).fetchone()
        if row is None or type(row[0]) is not int:
            raise KernelError("hook_store_corrupt", "Hook Registry代次索引损坏")
        return row[0] + 1

    def save_registry(self, registry: HookRegistrySnapshot) -> HookRegistrySnapshot:
        checked = HookRegistrySnapshot.model_validate_json(registry.model_dump_json())
        try:
            self._db.execute("BEGIN IMMEDIATE")
            if checked.generation != self.next_registry_generation(checked.registry_id):
                raise KernelError("hook_registry_conflict", "Hook Registry代次冲突")
            self._db.execute(
                "INSERT INTO hook_registries VALUES (?, ?, ?, ?)",
                (
                    checked.registry_id,
                    checked.generation,
                    checked.registry_sha256,
                    checked.model_dump_json(warnings="error"),
                ),
            )
            self._db.execute("COMMIT")
            return checked.model_copy(deep=True)
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load_registry(
        self,
        registry_id: str,
        *,
        generation: int | None = None,
        digest: str | None = None,
    ) -> HookRegistrySnapshot:
        where = ["registry_id = ?"]
        values: list[object] = [registry_id]
        if generation is not None:
            where.append("generation = ?")
            values.append(generation)
        if digest is not None:
            where.append("digest = ?")
            values.append(digest)
        order = "" if generation is not None else " ORDER BY generation DESC LIMIT 1"
        row = self._db.execute(
            "SELECT generation, digest, payload FROM hook_registries WHERE "
            + " AND ".join(where)
            + order,
            tuple(values),
        ).fetchone()
        if row is None:
            raise KernelError("hook_registry_not_found", "Hook Registry不存在")
        try:
            registry = HookRegistrySnapshot.model_validate_json(row[2])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("hook_store_corrupt", "Hook Registry正文损坏") from None
        if (
            registry.registry_id != registry_id
            or registry.generation != row[0]
            or registry.registry_sha256 != row[1]
        ):
            raise KernelError("hook_store_corrupt", "Hook Registry索引与正文不一致")
        return registry

    def begin(self, plan: HookRunPlan) -> HookRunSnapshot:
        checked = HookRunPlan.model_validate_json(plan.model_dump_json())
        try:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self._load_optional(checked.run_id)
            if existing is not None:
                if existing.plan != checked:
                    raise KernelError("hook_dispatch_conflict", "Hook Run身份已经绑定其他输入")
                self._db.execute("COMMIT")
                return existing
            registry = self.load_registry(
                checked.registry_id,
                generation=checked.registry_generation,
                digest=checked.registry_sha256,
            )
            if checked.definition not in registry.definitions:
                raise KernelError("hook_definition_changed", "Hook Run定义不属于Registry快照")
            event = build_hook_run_event(
                plan=checked,
                sequence=1,
                from_state=None,
                to_state="ready",
                occurred_at=utc_now(),
                previous_digest=None,
            )
            self._db.execute(
                "INSERT INTO hook_run_plans VALUES (?, ?, ?, ?, ?)",
                (
                    str(checked.run_id),
                    checked.plan_sha256,
                    checked.registry_id,
                    checked.registry_generation,
                    checked.model_dump_json(warnings="error"),
                ),
            )
            self._db.execute(
                "INSERT INTO hook_run_events VALUES (?, 1, ?, ?)",
                (str(checked.run_id), event.digest, event.model_dump_json(warnings="error")),
            )
            self._db.execute(
                "INSERT INTO hook_run_snapshots VALUES (?, 'ready', 1, ?, NULL, NULL, NULL, ?)",
                (str(checked.run_id), event.digest, event.occurred_at.isoformat()),
            )
            self._db.execute("COMMIT")
            return self.load(checked.run_id)
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def transition(
        self,
        run_id: UUID,
        *,
        expected: Collection[HookRunState],
        target: HookRunState,
        decision: HookDecisionKind | None = None,
        output_sha256: str | None = None,
        error_code: str | None = None,
    ) -> HookRunSnapshot:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            current = self._load_optional(run_id)
            if current is None:
                raise KernelError("hook_run_not_found", "Hook Run不存在")
            if current.state not in expected:
                raise KernelError("hook_run_state_conflict", "Hook Run状态冲突")
            if target not in _ALLOWED_TRANSITIONS[current.state]:
                raise KernelError("hook_run_state_conflict", "Hook Run状态转换无效")
            sequence = current.sequence + 1
            event = build_hook_run_event(
                plan=current.plan,
                sequence=sequence,
                from_state=current.state,
                to_state=target,
                occurred_at=utc_now(),
                previous_digest=current.last_event_digest,
                decision=decision,
                output_sha256=output_sha256,
                error_code=error_code,
            )
            self._db.execute(
                "INSERT INTO hook_run_events VALUES (?, ?, ?, ?)",
                (str(run_id), sequence, event.digest, event.model_dump_json(warnings="error")),
            )
            changed = self._db.execute(
                "UPDATE hook_run_snapshots SET state = ?, sequence = ?, last_event_digest = ?, "
                "decision = ?, output_digest = ?, error_code = ?, updated_at = ? "
                "WHERE run_id = ? AND state = ? AND sequence = ?",
                (
                    target,
                    sequence,
                    event.digest,
                    decision,
                    output_sha256,
                    error_code,
                    event.occurred_at.isoformat(),
                    str(run_id),
                    current.state,
                    current.sequence,
                ),
            ).rowcount
            if changed != 1:
                raise KernelError("hook_run_state_conflict", "Hook Run并发状态冲突")
            self._db.execute("COMMIT")
            return self.load(run_id)
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load(self, run_id: UUID) -> HookRunSnapshot:
        snapshot = self._load_optional(run_id)
        if snapshot is None:
            raise KernelError("hook_run_not_found", "Hook Run不存在")
        return snapshot

    def _load_optional(self, run_id: UUID) -> HookRunSnapshot | None:
        row = self._db.execute(
            "SELECT p.plan_digest, p.payload, s.state, s.sequence, s.last_event_digest, "
            "s.decision, s.output_digest, s.error_code, s.updated_at "
            "FROM hook_run_plans p JOIN hook_run_snapshots s ON p.run_id = s.run_id "
            "WHERE p.run_id = ?",
            (str(run_id),),
        ).fetchone()
        if row is None:
            return None
        try:
            plan = HookRunPlan.model_validate_json(row[1])
            snapshot = HookRunSnapshot(
                plan=plan,
                state=row[2],
                sequence=row[3],
                last_event_digest=row[4],
                decision=row[5],
                output_sha256=row[6],
                error_code=row[7],
                updated_at=datetime.fromisoformat(row[8]),
            )
            event_row = self._db.execute(
                "SELECT digest, payload FROM hook_run_events WHERE run_id = ? AND sequence = ?",
                (str(run_id), snapshot.sequence),
            ).fetchone()
            if event_row is None:
                raise ValueError
            event = HookRunEvent.model_validate_json(event_row[1])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("hook_store_corrupt", "Hook Run投影损坏") from None
        if (
            row[0] != plan.plan_sha256
            or plan.run_id != run_id
            or event_row[0] != snapshot.last_event_digest
            or event.digest != snapshot.last_event_digest
            or event.sequence != snapshot.sequence
            or event.to_state != snapshot.state
            or event.decision != snapshot.decision
            or event.output_sha256 != snapshot.output_sha256
            or event.error_code != snapshot.error_code
        ):
            raise KernelError("hook_store_corrupt", "Hook Run投影与事件不一致")
        return snapshot

    def events(self, run_id: UUID) -> tuple[HookRunEvent, ...]:
        current = self.load(run_id)
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM hook_run_events "
            "WHERE run_id = ? ORDER BY sequence",
            (str(run_id),),
        ).fetchall()
        try:
            events = tuple(HookRunEvent.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("hook_store_corrupt", "Hook Run事件损坏") from None
        previous: str | None = None
        state: HookRunState | None = None
        for index, (row, event) in enumerate(zip(rows, events, strict=True), start=1):
            if (
                row[0] != index
                or row[1] != event.digest
                or event.run_id != run_id
                or event.sequence != index
                or event.previous_digest != previous
                or event.from_state != state
            ):
                raise KernelError("hook_store_corrupt", "Hook Run事件链损坏")
            previous = event.digest
            state = event.to_state
        if (
            len(events) != current.sequence
            or previous != current.last_event_digest
            or state != current.state
        ):
            raise KernelError("hook_store_corrupt", "Hook Run事件链与投影不一致")
        return events

    def recover_interrupted(self) -> tuple[UUID, ...]:
        rows = self._db.execute(
            "SELECT run_id FROM hook_run_snapshots WHERE state = 'running' ORDER BY run_id"
        ).fetchall()
        recovered: list[UUID] = []
        for row in rows:
            try:
                run_id = UUID(row[0])
            except (ValueError, TypeError):
                raise KernelError("hook_store_corrupt", "Hook Run身份索引损坏") from None
            self.transition(
                run_id,
                expected={"running"},
                target="interrupted",
                error_code="hook_host_interrupted",
            )
            recovered.append(run_id)
        return tuple(recovered)

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteHookStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
