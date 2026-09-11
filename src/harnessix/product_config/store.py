"""以SQLite保存不可变配置快照、活动Profile CAS和连续Hash链审计。"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.contracts import (
    ConfigAuditEvent,
    ConfigAuditOperation,
    ConfigMigrationReceipt,
    ProductConfigSnapshot,
    ProviderFallbackDecision,
    config_audit_event_digest,
    provider_fallback_decision_digest,
)

_SCHEMA_VERSION = "1"


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


class SQLiteProductConfigStore:
    """保存不含Secret值的配置快照、激活事实与Fallback Hash链。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()
        self._closed = False
        self._prepare_path()
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if os.name == "posix":
                self.path.chmod(0o600)
            self._initialize()
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def _prepare_path(self) -> None:
        try:
            parent = self.path.parent
            if parent.exists() or _is_link_or_junction(parent):
                parent_info = parent.lstat()
                if not stat.S_ISDIR(parent_info.st_mode) or _is_link_or_junction(parent):
                    raise OSError
            else:
                parent.mkdir(parents=True, mode=0o700)
                parent_info = parent.lstat()
            if os.name == "posix" and (
                parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700
            ):
                raise OSError
            if not self.path.exists() and not _is_link_or_junction(self.path):
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                os.close(descriptor)
            info = self.path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (
                    os.name == "posix"
                    and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)
                )
            ):
                raise OSError
        except OSError:
            raise KernelError(
                "product_config_store_permissions",
                "产品配置存储路径权限或身份不安全",
            ) from None

    def _initialize(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS product_config_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT"
        )
        row = self._db.execute(
            "SELECT value FROM product_config_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO product_config_metadata VALUES ('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
        elif row[0] != _SCHEMA_VERSION:
            raise KernelError("product_config_store_version", "产品配置存储版本不受支持")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS product_config_snapshots (
                config_sha256 TEXT PRIMARY KEY,
                source_sha256 TEXT NOT NULL,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS product_config_events (
                sequence INTEGER PRIMARY KEY,
                digest TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS product_config_event_head (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS product_config_active (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                config_sha256 TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                FOREIGN KEY(config_sha256) REFERENCES product_config_snapshots(config_sha256)
            ) STRICT;
            CREATE TABLE IF NOT EXISTS provider_fallback_events (
                sequence INTEGER PRIMARY KEY,
                digest TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            ) STRICT;
            CREATE TABLE IF NOT EXISTS provider_fallback_head (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                sequence INTEGER NOT NULL,
                digest TEXT NOT NULL
            ) STRICT;
            """
        )

    def save_snapshot(self, snapshot: ProductConfigSnapshot) -> ProductConfigSnapshot:
        checked = ProductConfigSnapshot.model_validate_json(snapshot.model_dump_json())
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self.config_events()
            row = self._db.execute(
                "SELECT source_sha256, payload FROM product_config_snapshots "
                "WHERE config_sha256 = ?",
                (checked.config_sha256,),
            ).fetchone()
            if row is not None:
                existing = self._snapshot_row(row, checked.config_sha256)
                if existing.config != checked.config:
                    raise KernelError("product_config_store_corrupt", "配置摘要碰撞或正文损坏")
                self._db.execute("COMMIT")
                return existing
            self._insert_snapshot(checked)
            self._append_config_event(
                operation="loaded",
                config_sha256=checked.config_sha256,
            )
            self._db.execute("COMMIT")
            return checked.model_copy(deep=True)
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def record_migration(
        self, receipt: ConfigMigrationReceipt, snapshot: ProductConfigSnapshot
    ) -> ConfigAuditEvent:
        checked_receipt = ConfigMigrationReceipt.model_validate_json(receipt.model_dump_json())
        checked_snapshot = ProductConfigSnapshot.model_validate_json(snapshot.model_dump_json())
        if checked_receipt.target_sha256 != checked_snapshot.source_sha256:
            raise KernelError("product_config_migration_mismatch", "迁移收据与配置快照不匹配")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self.config_events()
            row = self._db.execute(
                "SELECT source_sha256, payload FROM product_config_snapshots "
                "WHERE config_sha256 = ?",
                (checked_snapshot.config_sha256,),
            ).fetchone()
            if row is None:
                self._insert_snapshot(checked_snapshot)
            elif self._snapshot_row(row, checked_snapshot.config_sha256).config != (
                checked_snapshot.config
            ):
                raise KernelError("product_config_store_corrupt", "迁移配置快照损坏")
            event = self._append_config_event(
                operation="migrated",
                config_sha256=checked_snapshot.config_sha256,
                migration_receipt_sha256=checked_receipt.receipt_sha256,
            )
            self._db.execute("COMMIT")
            return event
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def activate(
        self,
        snapshot: ProductConfigSnapshot,
        profile_id: str,
        *,
        expected_active_sha256: str | None,
        expected_active_profile: str | None = None,
    ) -> ConfigAuditEvent | None:
        checked = ProductConfigSnapshot.model_validate_json(snapshot.model_dump_json())
        try:
            checked.config.profile_chain(profile_id)
        except ValueError:
            raise KernelError("product_profile_not_found", "待激活Profile不存在") from None
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self.config_events()
            row = self._db.execute(
                "SELECT source_sha256, payload FROM product_config_snapshots "
                "WHERE config_sha256 = ?",
                (checked.config_sha256,),
            ).fetchone()
            if row is None:
                self._insert_snapshot(checked)
                self._append_config_event(
                    operation="loaded",
                    config_sha256=checked.config_sha256,
                )
            elif self._snapshot_row(row, checked.config_sha256).config != checked.config:
                raise KernelError("product_config_store_corrupt", "待激活配置快照损坏")
            active = self._db.execute(
                "SELECT config_sha256, profile_id FROM product_config_active WHERE singleton = 1"
            ).fetchone()
            if active is not None:
                active = self._validated_active(active)
            if active is not None and active == (checked.config_sha256, profile_id):
                self._db.execute("COMMIT")
                return None
            current = (None, None) if active is None else active
            expected = (expected_active_sha256, expected_active_profile)
            if current != expected:
                raise KernelError("product_config_conflict", "活动配置摘要与切换前提不一致")
            self._db.execute(
                "INSERT INTO product_config_active VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "config_sha256=excluded.config_sha256, profile_id=excluded.profile_id",
                (checked.config_sha256, profile_id),
            )
            event = self._append_config_event(
                operation="activated",
                config_sha256=checked.config_sha256,
                profile_id=profile_id,
                previous_active_sha256=current[0],
                previous_active_profile=current[1],
            )
            self._db.execute("COMMIT")
            return event
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def active(self) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT config_sha256, profile_id FROM product_config_active WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return None
        return self._validated_active(row)

    def load_snapshot(self, config_sha256: str) -> ProductConfigSnapshot:
        row = self._db.execute(
            "SELECT source_sha256, payload FROM product_config_snapshots WHERE config_sha256 = ?",
            (config_sha256,),
        ).fetchone()
        if row is None:
            raise KernelError("product_config_not_found", "产品配置快照不存在")
        return self._snapshot_row(row, config_sha256)

    def config_events(self) -> tuple[ConfigAuditEvent, ...]:
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM product_config_events ORDER BY sequence"
        ).fetchall()
        head = self._db.execute(
            "SELECT sequence, digest FROM product_config_event_head WHERE singleton = 1"
        ).fetchone()
        try:
            events = tuple(ConfigAuditEvent.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("product_config_store_corrupt", "配置事件正文损坏") from None
        self._verify_chain(rows, events, head)
        return events

    def record_fallback(
        self,
        *,
        config_sha256: str,
        thread_id: UUID,
        turn_id: UUID,
        step: int,
        from_profile: str,
        from_provider: str,
        to_profile: str,
        to_provider: str,
        failure_code: Literal["transport", "rate_limit", "provider_internal"],
    ) -> ProviderFallbackDecision:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self.config_events()
            self.fallback_events()
            snapshot = self.load_snapshot(config_sha256)
            profiles = {item.profile_id: item for item in snapshot.config.profiles}
            source = profiles.get(from_profile)
            target = profiles.get(to_profile)
            if (
                source is None
                or target is None
                or source.provider_id != from_provider
                or target.provider_id != to_provider
                or not any(
                    any(
                        left.profile_id == from_profile and right.profile_id == to_profile
                        for left, right in pairwise(chain)
                    )
                    for chain in (
                        snapshot.config.profile_chain(profile.profile_id)
                        for profile in snapshot.config.profiles
                    )
                )
            ):
                raise KernelError("product_config_fallback_invalid", "Fallback决策不属于配置图")
            row = self._db.execute(
                "SELECT sequence, digest FROM provider_fallback_head WHERE singleton = 1"
            ).fetchone()
            sequence = 1 if row is None else row[0] + 1
            previous = None if row is None else row[1]
            candidate = ProviderFallbackDecision.model_construct(
                _fields_set=None,
                sequence=sequence,
                config_sha256=config_sha256,
                thread_id=thread_id,
                turn_id=turn_id,
                step=step,
                from_profile=from_profile,
                from_provider=from_provider,
                to_profile=to_profile,
                to_provider=to_provider,
                failure_code=failure_code,
                response_exposed=False,
                tool_call_exposed=False,
                previous_digest=previous,
                occurred_at=utc_now(),
                digest="0" * 64,
            )
            event = ProviderFallbackDecision(
                **candidate.model_dump(exclude={"digest"}),
                digest=provider_fallback_decision_digest(candidate),
            )
            self._db.execute(
                "INSERT INTO provider_fallback_events VALUES (?, ?, ?)",
                (sequence, event.digest, event.model_dump_json(warnings="error")),
            )
            self._db.execute(
                "INSERT INTO provider_fallback_head VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET sequence=excluded.sequence, "
                "digest=excluded.digest",
                (sequence, event.digest),
            )
            self._db.execute("COMMIT")
            return event
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def fallback_events(self) -> tuple[ProviderFallbackDecision, ...]:
        rows = self._db.execute(
            "SELECT sequence, digest, payload FROM provider_fallback_events ORDER BY sequence"
        ).fetchall()
        head = self._db.execute(
            "SELECT sequence, digest FROM provider_fallback_head WHERE singleton = 1"
        ).fetchone()
        try:
            events = tuple(ProviderFallbackDecision.model_validate_json(row[2]) for row in rows)
        except (ValidationError, ValueError, TypeError):
            raise KernelError("product_config_store_corrupt", "Fallback事件正文损坏") from None
        self._verify_chain(rows, events, head)
        return events

    def _insert_snapshot(self, snapshot: ProductConfigSnapshot) -> None:
        self._db.execute(
            "INSERT INTO product_config_snapshots VALUES (?, ?, ?)",
            (
                snapshot.config_sha256,
                snapshot.source_sha256,
                snapshot.model_dump_json(warnings="error"),
            ),
        )

    def _validated_active(self, row: tuple[object, ...]) -> tuple[str, str]:
        if len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str):
            raise KernelError("product_config_store_corrupt", "活动配置索引损坏")
        snapshot = self.load_snapshot(row[0])
        try:
            snapshot.config.profile_chain(row[1])
        except ValueError:
            raise KernelError("product_config_store_corrupt", "活动Profile索引损坏") from None
        return row[0], row[1]

    def _snapshot_row(self, row: tuple[object, ...], config_sha256: str) -> ProductConfigSnapshot:
        if len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str):
            raise KernelError("product_config_store_corrupt", "配置快照列类型损坏")
        try:
            snapshot = ProductConfigSnapshot.model_validate_json(row[1])
        except (ValidationError, ValueError, TypeError):
            raise KernelError("product_config_store_corrupt", "配置快照正文损坏") from None
        if snapshot.config_sha256 != config_sha256 or snapshot.source_sha256 != row[0]:
            raise KernelError("product_config_store_corrupt", "配置快照索引与正文不一致")
        return snapshot

    def _append_config_event(
        self,
        *,
        operation: ConfigAuditOperation,
        config_sha256: str,
        profile_id: str | None = None,
        previous_active_sha256: str | None = None,
        previous_active_profile: str | None = None,
        migration_receipt_sha256: str | None = None,
    ) -> ConfigAuditEvent:
        row = self._db.execute(
            "SELECT sequence, digest FROM product_config_event_head WHERE singleton = 1"
        ).fetchone()
        sequence = 1 if row is None else row[0] + 1
        previous = None if row is None else row[1]
        candidate = ConfigAuditEvent.model_construct(
            _fields_set=None,
            sequence=sequence,
            operation=operation,
            config_sha256=config_sha256,
            profile_id=profile_id,
            previous_active_sha256=previous_active_sha256,
            previous_active_profile=previous_active_profile,
            migration_receipt_sha256=migration_receipt_sha256,
            previous_digest=previous,
            occurred_at=utc_now(),
            digest="0" * 64,
        )
        event = ConfigAuditEvent(
            **candidate.model_dump(exclude={"digest"}),
            digest=config_audit_event_digest(candidate),
        )
        self._db.execute(
            "INSERT INTO product_config_events VALUES (?, ?, ?)",
            (sequence, event.digest, event.model_dump_json(warnings="error")),
        )
        self._db.execute(
            "INSERT INTO product_config_event_head VALUES (1, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET sequence=excluded.sequence, "
            "digest=excluded.digest",
            (sequence, event.digest),
        )
        return event

    @staticmethod
    def _verify_chain(
        rows: Sequence[tuple[object, ...]],
        events: Sequence[ConfigAuditEvent | ProviderFallbackDecision],
        head: tuple[object, ...] | None,
    ) -> None:
        previous: str | None = None
        for index, (row, event) in enumerate(zip(rows, events, strict=True), start=1):
            if (
                row[0] != index
                or row[1] != event.digest
                or event.sequence != index
                or event.previous_digest != previous
            ):
                raise KernelError("product_config_store_corrupt", "产品配置事件链损坏")
            previous = event.digest
        if (not events) != (head is None):
            raise KernelError("product_config_store_corrupt", "产品配置事件头缺失")
        if events and (head is None or head[0] != len(events) or head[1] != previous):
            raise KernelError("product_config_store_corrupt", "产品配置事件头与链不一致")

    def close(self) -> None:
        if not self._closed:
            self._db.close()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
