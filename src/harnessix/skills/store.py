"""以SQLite保存Skill目录快照和连续Hash链访问事件，不保存资源正文。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.execution.contracts import canonical_digest
from harnessix.skills.contracts import (
    SkillAccessEvent,
    SkillAccessOperation,
    SkillAccessOutcome,
    SkillCatalogSnapshot,
    build_skill_access_event,
)

_SCHEMA_VERSION = "1"


class SQLiteSkillStore:
    """保存无正文Skill目录与Hash链访问事实。"""

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
            "CREATE TABLE IF NOT EXISTS skill_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM skill_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO skill_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("skill_store_version", "Skill存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS skill_catalogs (
                catalog_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(catalog_id, generation)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS skill_access_events (
                catalog_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(catalog_id, sequence),
                UNIQUE(digest)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS skill_access_heads (
                catalog_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL
            ) STRICT;
            """
        )

    def next_generation(self, catalog_id: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(generation), 0) FROM skill_catalogs WHERE catalog_id = ?",
            (catalog_id,),
        ).fetchone()
        if row is None or type(row[0]) is not int:
            raise KernelError("skill_store_corrupt", "Skill目录代次索引损坏")
        return row[0] + 1

    def save_catalog(self, catalog: SkillCatalogSnapshot) -> SkillCatalogSnapshot:
        checked = SkillCatalogSnapshot.model_validate_json(catalog.model_dump_json())
        try:
            self._db.execute("BEGIN IMMEDIATE")
            expected = self.next_generation(checked.catalog_id)
            if checked.generation != expected:
                raise KernelError("skill_catalog_conflict", "Skill目录代次冲突")
            self._db.execute(
                "INSERT INTO skill_catalogs VALUES (?, ?, ?, ?)",
                (
                    checked.catalog_id,
                    checked.generation,
                    checked.catalog_sha256,
                    checked.model_dump_json(warnings="error"),
                ),
            )
            self._db.execute("COMMIT")
            return checked.model_copy(deep=True)
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def load_catalog(
        self,
        catalog_id: str,
        *,
        generation: int | None = None,
        digest: str | None = None,
    ) -> SkillCatalogSnapshot:
        if generation is not None and digest is not None:
            row = self._db.execute(
                "SELECT generation, digest, payload FROM skill_catalogs "
                "WHERE catalog_id = ? AND generation = ? AND digest = ?",
                (catalog_id, generation, digest),
            ).fetchone()
        elif generation is not None:
            row = self._db.execute(
                "SELECT generation, digest, payload FROM skill_catalogs "
                "WHERE catalog_id = ? AND generation = ?",
                (catalog_id, generation),
            ).fetchone()
        elif digest is not None:
            row = self._db.execute(
                "SELECT generation, digest, payload FROM skill_catalogs "
                "WHERE catalog_id = ? AND digest = ? ORDER BY generation DESC LIMIT 1",
                (catalog_id, digest),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT generation, digest, payload FROM skill_catalogs "
                "WHERE catalog_id = ? ORDER BY generation DESC LIMIT 1",
                (catalog_id,),
            ).fetchone()
        if row is None:
            raise KernelError("skill_catalog_not_found", "Skill目录不存在")
        try:
            catalog = SkillCatalogSnapshot.model_validate_json(row[2])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("skill_store_corrupt", "Skill目录正文损坏") from None
        if (
            catalog.catalog_id != catalog_id
            or catalog.generation != row[0]
            or catalog.catalog_sha256 != row[1]
        ):
            raise KernelError("skill_store_corrupt", "Skill目录索引与正文不一致")
        return catalog

    def record_access(
        self,
        *,
        catalog: SkillCatalogSnapshot,
        operation: SkillAccessOperation,
        manifest_sha256: str,
        resource_path: str | None,
        outcome: SkillAccessOutcome,
        result_sha256: str | None = None,
        error_code: str | None = None,
    ) -> SkillAccessEvent:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            stored = self.load_catalog(
                catalog.catalog_id,
                generation=catalog.generation,
                digest=catalog.catalog_sha256,
            )
            if stored != catalog:
                raise KernelError("skill_store_corrupt", "Skill访问绑定目录不一致")
            row = self._db.execute(
                "SELECT sequence, digest FROM skill_access_heads WHERE catalog_id = ?",
                (catalog.catalog_id,),
            ).fetchone()
            sequence = 1 if row is None else row[0] + 1
            previous = None if row is None else row[1]
            if type(sequence) is not int or (
                previous is not None and not isinstance(previous, str)
            ):
                raise KernelError("skill_store_corrupt", "Skill访问事件头损坏")
            event = build_skill_access_event(
                catalog_id=catalog.catalog_id,
                sequence=sequence,
                generation=catalog.generation,
                catalog_sha256=catalog.catalog_sha256,
                operation=operation,
                manifest_sha256=manifest_sha256,
                resource_path_sha256=(
                    canonical_digest(resource_path) if resource_path is not None else None
                ),
                outcome=outcome,
                result_sha256=result_sha256,
                error_code=error_code,
                occurred_at=utc_now(),
                previous_digest=previous,
            )
            self._db.execute(
                "INSERT INTO skill_access_events VALUES (?, ?, ?, ?)",
                (
                    catalog.catalog_id,
                    sequence,
                    event.digest,
                    event.model_dump_json(warnings="error"),
                ),
            )
            self._db.execute(
                "INSERT INTO skill_access_heads VALUES (?, ?, ?) "
                "ON CONFLICT(catalog_id) DO UPDATE SET sequence=excluded.sequence, "
                "digest=excluded.digest",
                (catalog.catalog_id, sequence, event.digest),
            )
            self._db.execute("COMMIT")
            return event
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def events(self, catalog_id: str) -> tuple[SkillAccessEvent, ...]:
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM skill_access_events "
            "WHERE catalog_id = ? ORDER BY sequence",
            (catalog_id,),
        ).fetchall()
        head = self._db.execute(
            "SELECT sequence, digest FROM skill_access_heads WHERE catalog_id = ?",
            (catalog_id,),
        ).fetchone()
        try:
            events = tuple(SkillAccessEvent.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("skill_store_corrupt", "Skill访问事件正文损坏") from None
        previous: str | None = None
        for index, (row, event) in enumerate(zip(rows, events, strict=True), start=1):
            if (
                row[0] != index
                or row[1] != event.digest
                or event.catalog_id != catalog_id
                or event.sequence != index
                or event.previous_digest != previous
            ):
                raise KernelError("skill_store_corrupt", "Skill访问事件链损坏")
            previous = event.digest
        if (not events) != (head is None):
            raise KernelError("skill_store_corrupt", "Skill访问事件头缺失")
        if events and (head[0] != len(events) or head[1] != previous):
            raise KernelError("skill_store_corrupt", "Skill访问事件头与链不一致")
        return events

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> SQLiteSkillStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
