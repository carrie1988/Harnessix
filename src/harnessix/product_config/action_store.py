"""在产品配置SQLite事务中保存独立Action配置、活动CAS与恢复报告。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.action_contracts import (
    ProductActionConfigAuditEvent,
    ProductActionConfigOperation,
    ProductActionConfigSnapshot,
    ProductActionStartupRecoveryReport,
    product_action_config_audit_event_digest,
)
from harnessix.product_config.contracts import ConfigAuditEvent, ProductConfigSnapshot
from harnessix.product_config.store import SQLiteProductConfigStore
from harnessix.trusted_actions.recovery_contracts import ActionRecoveryScanReport


class SQLiteProductRuntimeConfigStore(SQLiteProductConfigStore):
    """协调模型配置与Action配置，使两个活动指针在同一事务中切换。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        try:
            _initialize_action_store(self._db)
        except BaseException:
            self.close()
            raise

    def save_action_snapshot(
        self, snapshot: ProductActionConfigSnapshot
    ) -> ProductActionConfigSnapshot:
        return _save_action_snapshot(self._db, snapshot)

    def active_action(self) -> str | None:
        return _active_action(self._db)

    def load_action_snapshot(self, config_sha256: str) -> ProductActionConfigSnapshot:
        return _load_action_snapshot(self._db, config_sha256)

    def action_config_events(self) -> tuple[ProductActionConfigAuditEvent, ...]:
        return _action_config_events(self._db)

    def save_action_recovery_report(
        self, report: ProductActionStartupRecoveryReport
    ) -> ProductActionStartupRecoveryReport:
        return _save_recovery_report(self._db, report)

    def action_recovery_reports(self) -> tuple[ProductActionStartupRecoveryReport, ...]:
        return _recovery_reports(self._db)

    def save_action_recovery_scan(
        self, report: ActionRecoveryScanReport
    ) -> ActionRecoveryScanReport:
        return _save_recovery_scan(self._db, report)

    def action_recovery_scans(self) -> tuple[ActionRecoveryScanReport, ...]:
        return _recovery_scans(self._db)

    def activate_runtime(
        self,
        snapshot: ProductConfigSnapshot,
        profile_id: str,
        action_snapshot: ProductActionConfigSnapshot,
        *,
        expected_active_sha256: str | None,
        expected_active_profile: str | None = None,
        expected_active_action_sha256: str | None = None,
    ) -> tuple[ConfigAuditEvent | None, ProductActionConfigAuditEvent | None]:
        return _activate_runtime(
            self,
            snapshot,
            profile_id,
            action_snapshot,
            expected_product=(expected_active_sha256, expected_active_profile),
            expected_action=expected_active_action_sha256,
        )


def _initialize_action_store(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS product_action_config_snapshots (
            config_sha256 TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            payload TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS product_action_config_events (
            sequence INTEGER PRIMARY KEY,
            digest TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS product_action_config_event_head (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            sequence INTEGER NOT NULL,
            digest TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS product_action_config_active (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            config_sha256 TEXT NOT NULL,
            FOREIGN KEY(config_sha256) REFERENCES product_action_config_snapshots(config_sha256)
        ) STRICT;
        CREATE TABLE IF NOT EXISTS product_action_recovery_reports (
            report_sha256 TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS product_action_recovery_scans (
            report_sha256 TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        ) STRICT;
        """
    )


def _save_action_snapshot(
    database: sqlite3.Connection,
    snapshot: ProductActionConfigSnapshot,
) -> ProductActionConfigSnapshot:
    checked = ProductActionConfigSnapshot.model_validate_json(snapshot.model_dump_json())
    try:
        database.execute("BEGIN IMMEDIATE")
        _action_config_events(database)
        existing = _load_optional_action_snapshot(database, checked.config_sha256)
        if existing is not None:
            if existing.config != checked.config:
                raise KernelError(
                    "product_action_config_store_corrupt", "Action配置摘要碰撞或正文损坏"
                )
            database.execute("COMMIT")
            return existing
        _ensure_action_snapshot(database, checked)
        database.execute("COMMIT")
        return checked.model_copy(deep=True)
    except BaseException:
        _rollback(database)
        raise


def _ensure_action_snapshot(
    database: sqlite3.Connection,
    snapshot: ProductActionConfigSnapshot,
) -> None:
    existing = _load_optional_action_snapshot(database, snapshot.config_sha256)
    if existing is None:
        database.execute(
            "INSERT INTO product_action_config_snapshots VALUES (?, ?, ?)",
            (
                snapshot.config_sha256,
                snapshot.source_sha256,
                snapshot.model_dump_json(warnings="error"),
            ),
        )
        _append_action_event(database, operation="loaded", config_sha256=snapshot.config_sha256)
    elif existing.config != snapshot.config:
        raise KernelError("product_action_config_store_corrupt", "待激活Action配置快照损坏")


def _active_action(database: sqlite3.Connection) -> str | None:
    row = database.execute(
        "SELECT config_sha256 FROM product_action_config_active WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    if len(row) != 1 or not isinstance(row[0], str):
        raise KernelError("product_action_config_store_corrupt", "活动Action配置索引损坏")
    _load_action_snapshot(database, row[0])
    return row[0]


def _load_action_snapshot(
    database: sqlite3.Connection,
    config_sha256: str,
) -> ProductActionConfigSnapshot:
    snapshot = _load_optional_action_snapshot(database, config_sha256)
    if snapshot is None:
        raise KernelError("product_action_config_not_found", "Product Action配置快照不存在")
    return snapshot


def _load_optional_action_snapshot(
    database: sqlite3.Connection,
    config_sha256: str,
) -> ProductActionConfigSnapshot | None:
    row = database.execute(
        "SELECT source_sha256, payload FROM product_action_config_snapshots "
        "WHERE config_sha256 = ?",
        (config_sha256,),
    ).fetchone()
    if row is None:
        return None
    if len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str):
        raise KernelError("product_action_config_store_corrupt", "Action配置快照列类型损坏")
    try:
        snapshot = ProductActionConfigSnapshot.model_validate_json(row[1])
    except (ValidationError, ValueError, TypeError):
        raise KernelError("product_action_config_store_corrupt", "Action配置快照正文损坏") from None
    if snapshot.config_sha256 != config_sha256 or snapshot.source_sha256 != row[0]:
        raise KernelError("product_action_config_store_corrupt", "Action配置快照索引与正文不一致")
    return snapshot


def _action_config_events(
    database: sqlite3.Connection,
) -> tuple[ProductActionConfigAuditEvent, ...]:
    rows = database.execute(
        "SELECT sequence, digest, payload FROM product_action_config_events ORDER BY sequence"
    ).fetchall()
    head = database.execute(
        "SELECT sequence, digest FROM product_action_config_event_head WHERE singleton = 1"
    ).fetchone()
    try:
        events = tuple(ProductActionConfigAuditEvent.model_validate_json(row[2]) for row in rows)
    except (ValidationError, ValueError, TypeError):
        raise KernelError("product_action_config_store_corrupt", "Action配置事件正文损坏") from None
    _verify_action_chain(rows, events, head)
    return events


def _append_action_event(
    database: sqlite3.Connection,
    *,
    operation: ProductActionConfigOperation,
    config_sha256: str,
    previous_active_sha256: str | None = None,
) -> ProductActionConfigAuditEvent:
    row = database.execute(
        "SELECT sequence, digest FROM product_action_config_event_head WHERE singleton = 1"
    ).fetchone()
    sequence = 1 if row is None else row[0] + 1
    previous = None if row is None else row[1]
    candidate = ProductActionConfigAuditEvent.model_construct(
        _fields_set=None,
        sequence=sequence,
        operation=operation,
        config_sha256=config_sha256,
        previous_active_sha256=previous_active_sha256,
        previous_digest=previous,
        occurred_at=utc_now(),
        digest="0" * 64,
    )
    event = ProductActionConfigAuditEvent(
        **candidate.model_dump(exclude={"digest"}),
        digest=product_action_config_audit_event_digest(candidate),
    )
    database.execute(
        "INSERT INTO product_action_config_events VALUES (?, ?, ?)",
        (sequence, event.digest, event.model_dump_json(warnings="error")),
    )
    database.execute(
        "INSERT INTO product_action_config_event_head VALUES (1, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET sequence=excluded.sequence, "
        "digest=excluded.digest",
        (sequence, event.digest),
    )
    return event


def _verify_action_chain(
    rows: Sequence[tuple[object, ...]],
    events: Sequence[ProductActionConfigAuditEvent],
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
            raise KernelError("product_action_config_store_corrupt", "Action配置事件链损坏")
        previous = event.digest
    if (not events) != (head is None):
        raise KernelError("product_action_config_store_corrupt", "Action配置事件头缺失")
    if events and (head is None or head[0] != len(events) or head[1] != previous):
        raise KernelError("product_action_config_store_corrupt", "Action配置事件头与链不一致")


def _save_recovery_report(
    database: sqlite3.Connection,
    report: ProductActionStartupRecoveryReport,
) -> ProductActionStartupRecoveryReport:
    checked = ProductActionStartupRecoveryReport.model_validate_json(report.model_dump_json())
    payload = checked.model_dump_json(warnings="error")
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT payload FROM product_action_recovery_reports WHERE report_sha256 = ?",
            (checked.report_sha256,),
        ).fetchone()
        if row is None:
            database.execute(
                "INSERT INTO product_action_recovery_reports VALUES (?, ?)",
                (checked.report_sha256, payload),
            )
        elif len(row) != 1 or row[0] != payload:
            raise KernelError(
                "product_action_config_store_corrupt", "Action恢复报告摘要碰撞或正文损坏"
            )
        database.execute("COMMIT")
        return checked.model_copy(deep=True)
    except BaseException:
        _rollback(database)
        raise


def _recovery_reports(
    database: sqlite3.Connection,
) -> tuple[ProductActionStartupRecoveryReport, ...]:
    rows = database.execute(
        "SELECT report_sha256, payload FROM product_action_recovery_reports ORDER BY rowid"
    ).fetchall()
    reports: list[ProductActionStartupRecoveryReport] = []
    try:
        for digest, payload in rows:
            report = ProductActionStartupRecoveryReport.model_validate_json(payload)
            if report.report_sha256 != digest:
                raise ValueError
            reports.append(report)
    except (ValidationError, ValueError, TypeError):
        raise KernelError("product_action_config_store_corrupt", "Action恢复报告正文损坏") from None
    return tuple(reports)


def _save_recovery_scan(
    database: sqlite3.Connection,
    report: ActionRecoveryScanReport,
) -> ActionRecoveryScanReport:
    checked = ActionRecoveryScanReport.model_validate_json(report.model_dump_json())
    payload = checked.model_dump_json(warnings="error")
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT payload FROM product_action_recovery_scans WHERE report_sha256 = ?",
            (checked.report_sha256,),
        ).fetchone()
        if row is None:
            database.execute(
                "INSERT INTO product_action_recovery_scans VALUES (?, ?)",
                (checked.report_sha256, payload),
            )
        elif len(row) != 1 or row[0] != payload:
            raise KernelError(
                "product_action_config_store_corrupt",
                "Action恢复扫描摘要碰撞或正文损坏",
            )
        database.execute("COMMIT")
        return checked.model_copy(deep=True)
    except BaseException:
        _rollback(database)
        raise


def _recovery_scans(database: sqlite3.Connection) -> tuple[ActionRecoveryScanReport, ...]:
    rows = database.execute(
        "SELECT report_sha256, payload FROM product_action_recovery_scans ORDER BY rowid"
    ).fetchall()
    reports: list[ActionRecoveryScanReport] = []
    try:
        for digest, payload in rows:
            report = ActionRecoveryScanReport.model_validate_json(payload)
            if report.report_sha256 != digest:
                raise ValueError
            reports.append(report)
    except (ValidationError, ValueError, TypeError):
        raise KernelError(
            "product_action_config_store_corrupt",
            "Action恢复扫描正文损坏",
        ) from None
    return tuple(reports)


def _activate_runtime(
    store: SQLiteProductRuntimeConfigStore,
    snapshot: ProductConfigSnapshot,
    profile_id: str,
    action_snapshot: ProductActionConfigSnapshot,
    *,
    expected_product: tuple[str | None, str | None],
    expected_action: str | None,
) -> tuple[ConfigAuditEvent | None, ProductActionConfigAuditEvent | None]:
    checked, checked_action = _validate_inputs(snapshot, profile_id, action_snapshot)
    try:
        store._db.execute("BEGIN IMMEDIATE")
        store.config_events()
        _action_config_events(store._db)
        _ensure_product_snapshot(store, checked)
        _ensure_action_snapshot(store._db, checked_action)
        current_product = _current_product(store)
        current_action = _active_action(store._db)
        product_changed = current_product != (checked.config_sha256, profile_id)
        action_changed = current_action != checked_action.config_sha256
        _validate_cas(
            current_product,
            current_action,
            product_changed=product_changed,
            action_changed=action_changed,
            expected_product=expected_product,
            expected_action=expected_action,
        )
        product_event = _activate_product(
            store, checked, profile_id, current_product, changed=product_changed
        )
        action_event = (
            _activate_action(store._db, checked_action, current_action) if action_changed else None
        )
        store._db.execute("COMMIT")
        return product_event, action_event
    except BaseException:
        _rollback(store._db)
        raise


def _validate_inputs(
    snapshot: ProductConfigSnapshot,
    profile_id: str,
    action_snapshot: ProductActionConfigSnapshot,
) -> tuple[ProductConfigSnapshot, ProductActionConfigSnapshot]:
    checked = ProductConfigSnapshot.model_validate_json(snapshot.model_dump_json())
    checked_action = ProductActionConfigSnapshot.model_validate_json(
        action_snapshot.model_dump_json()
    )
    try:
        checked.config.profile_chain(profile_id)
    except ValueError:
        raise KernelError("product_profile_not_found", "待激活Profile不存在") from None
    return checked, checked_action


def _current_product(
    store: SQLiteProductRuntimeConfigStore,
) -> tuple[str | None, str | None]:
    row = store._db.execute(
        "SELECT config_sha256, profile_id FROM product_config_active WHERE singleton = 1"
    ).fetchone()
    return (None, None) if row is None else store._validated_active(row)


def _validate_cas(
    current_product: tuple[str | None, str | None],
    current_action: str | None,
    *,
    product_changed: bool,
    action_changed: bool,
    expected_product: tuple[str | None, str | None],
    expected_action: str | None,
) -> None:
    if product_changed and current_product != expected_product:
        raise KernelError("product_config_conflict", "活动配置摘要与切换前提不一致")
    if action_changed and current_action != expected_action:
        raise KernelError("product_action_config_conflict", "活动Action配置摘要与切换前提不一致")


def _activate_product(
    store: SQLiteProductRuntimeConfigStore,
    snapshot: ProductConfigSnapshot,
    profile_id: str,
    current: tuple[str | None, str | None],
    *,
    changed: bool,
) -> ConfigAuditEvent | None:
    if not changed:
        return None
    store._db.execute(
        "INSERT INTO product_config_active VALUES (1, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "config_sha256=excluded.config_sha256, profile_id=excluded.profile_id",
        (snapshot.config_sha256, profile_id),
    )
    return store._append_config_event(
        operation="activated",
        config_sha256=snapshot.config_sha256,
        profile_id=profile_id,
        previous_active_sha256=current[0],
        previous_active_profile=current[1],
    )


def _activate_action(
    database: sqlite3.Connection,
    snapshot: ProductActionConfigSnapshot,
    current: str | None,
) -> ProductActionConfigAuditEvent:
    database.execute(
        "INSERT INTO product_action_config_active VALUES (1, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET config_sha256=excluded.config_sha256",
        (snapshot.config_sha256,),
    )
    return _append_action_event(
        database,
        operation="activated",
        config_sha256=snapshot.config_sha256,
        previous_active_sha256=current,
    )


def _ensure_product_snapshot(
    store: SQLiteProductRuntimeConfigStore,
    snapshot: ProductConfigSnapshot,
) -> None:
    row = store._db.execute(
        "SELECT source_sha256, payload FROM product_config_snapshots WHERE config_sha256 = ?",
        (snapshot.config_sha256,),
    ).fetchone()
    if row is None:
        store._insert_snapshot(snapshot)
        store._append_config_event(operation="loaded", config_sha256=snapshot.config_sha256)
    elif store._snapshot_row(row, snapshot.config_sha256).config != snapshot.config:
        raise KernelError("product_config_store_corrupt", "待激活配置快照损坏")


def _rollback(database: sqlite3.Connection) -> None:
    if database.in_transaction:
        database.execute("ROLLBACK")
