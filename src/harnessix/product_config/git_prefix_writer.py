"""原GitDB事务内认证新增事实与全集尾锚；不签旧库、不提交或执行Git效果。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from weakref import ReferenceType, ref

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import PublicOutputProtection
from harnessix.delivery.git_store_schema_v2 import verify_git_store_v2_schema
from harnessix.product_config.git_prefix_catalog import GitPrefixCatalog, encode_git_prefix_catalog
from harnessix.product_config.git_prefix_reader import (
    _anchor,
    build_git_prefix_catalog,
    read_git_prefix_catalog,
)
from harnessix.product_config.git_prefix_records import record_bodies, unproven
from harnessix.product_config.git_prefix_rows import GitPrefixRows, capture_git_prefix_rows
from harnessix.product_config.git_prefix_sql import (
    _register_git_prefix_write_window,
    _require_issued_git_prefix_window,
    git_prefix_transaction_epoch,
    require_git_prefix_sql_window,
)
from harnessix.session.git_prefix_contracts import GitStorePrefixAnchorClaims
from harnessix.session.git_prefix_publication import GitStorePrefixAuthority
from harnessix.session.git_publication_contracts import (
    GitDeliveryRecordClaims,
    snapshot_git_delivery_claims,
)
from harnessix.session.store_publication import GitPublicationAuthority, extend_prefix


def _writable_transaction(database: sqlite3.Connection) -> None:
    require_git_prefix_sql_window(database)
    if database.execute("PRAGMA query_only").fetchone() == (1,):
        raise KernelError("git_delivery_store_read_only", "Git交付只读账本不接受写入")
    if not database.in_transaction:
        raise KernelError("git_delivery_store_transaction_required", "Git认证写入需要已有事务")
    verify_git_store_v2_schema(database)
    try:
        # URI mode=ro不会反映在query_only中；真实表上零行UPDATE核验底层可写性。
        database.execute("UPDATE main.git_delivery_metadata SET value=value WHERE 0")
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_READONLY:
            raise KernelError("git_delivery_store_read_only", "Git交付只读账本不接受写入") from None
        raise unproven() from None


def _save_anchor(database: sqlite3.Connection, catalog: GitPrefixCatalog, seal: bytes) -> None:
    body = encode_git_prefix_catalog(catalog)
    database.execute(
        "INSERT INTO git_prefix_anchor VALUES (1,?,?,?,?,?,?) "
        "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
        "genesis_epoch=excluded.genesis_epoch, body_bytes=excluded.body_bytes, "
        "body_sha256=excluded.body_sha256, payload=excluded.payload, seal=excluded.seal",
        (
            catalog.revision,
            str(catalog.genesis_epoch),
            len(body),
            sha256(body).hexdigest(),
            body.decode("utf-8"),
            seal,
        ),
    )


async def initialize_git_prefix_genesis(
    database: sqlite3.Connection,
    authority: GitPublicationAuthority,
    prefix_authority: GitStorePrefixAuthority,
    protection: PublicOutputProtection,
    *,
    cancel: CancelToken,
) -> GitPrefixCatalog:
    """精确空v2首次认证；已有尾锚只验真，未知/非空未认证行绝不批量补签。"""
    _writable_transaction(database)
    cancel.checkpoint()
    epoch = git_prefix_transaction_epoch(database)
    verify_git_store_v2_schema(database)
    if database.execute("SELECT 1 FROM main.git_prefix_anchor LIMIT 1").fetchone() is not None:
        return read_git_prefix_catalog(
            database, authority, prefix_authority, checkpoint=cancel.checkpoint
        )
    rows = capture_git_prefix_rows(database, checkpoint=cancel.checkpoint)
    if any(values for name, values in rows.rows if name != "git_delivery_metadata"):
        raise KernelError("git_delivery_store_legacy_unproven", "Git既有未认证事实不能升代补签")
    if authority.identity() != prefix_authority.identity():
        raise unproven()
    from uuid import uuid4

    catalog = build_git_prefix_catalog(rows, authority, uuid4(), checkpoint=cancel.checkpoint)
    seal = await prefix_authority.issue(
        GitStorePrefixAnchorClaims(
            schema_version="2", store_genesis_epoch=catalog.genesis_epoch, revision=0
        ),
        encode_git_prefix_catalog(catalog),
        protection,
        cancel=cancel,
    )
    _writable_transaction(database)
    if git_prefix_transaction_epoch(database) != epoch:
        raise unproven()
    if (
        capture_git_prefix_rows(database, checkpoint=cancel.checkpoint) != rows
        or database.execute("SELECT 1 FROM git_prefix_anchor LIMIT 1").fetchone() is not None
    ):
        raise unproven()
    prefix_authority.verify(
        seal,
        GitStorePrefixAnchorClaims(
            schema_version="2", store_genesis_epoch=catalog.genesis_epoch, revision=0
        ),
        encode_git_prefix_catalog(catalog),
        checkpoint=cancel.checkpoint,
    )
    _save_anchor(database, catalog, seal)
    return catalog


@dataclass(frozen=True, slots=True, weakref_slot=True)
class GitPrefixWriteWindow:
    """同一连接、原已认证前缀及私有实例见证；不是跨库Owner或业务批准。"""

    database: sqlite3.Connection = field(repr=False)
    catalog: GitPrefixCatalog
    rows: GitPrefixRows = field(repr=False)
    anchor: tuple[object, ...] = field(repr=False)
    transaction_epoch: tuple[object, int] = field(repr=False)
    _witness: object = field(default=None, repr=False, compare=False)
    _used: bool = field(default=False, repr=False, compare=False)


def begin_git_prefix_write(
    database: sqlite3.Connection,
    authority: GitPublicationAuthority,
    prefix_authority: GitStorePrefixAuthority,
    *,
    checkpoint: Callable[[], None],
) -> GitPrefixWriteWindow:
    """在业务修改之前验证完整原前缀；返回不可复制的单次同事务发布窗口。"""
    _writable_transaction(database)
    epoch = git_prefix_transaction_epoch(database)
    changes = database.total_changes
    catalog = read_git_prefix_catalog(database, authority, prefix_authority, checkpoint=checkpoint)
    rows = capture_git_prefix_rows(database, checkpoint=checkpoint)
    if rows.tables != catalog.tables:
        raise unproven()
    anchor = database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    claims, body, seal = _anchor(database)
    prefix_authority.verify(seal, claims, body, checkpoint=checkpoint)
    if encode_git_prefix_catalog(catalog) != body or database.total_changes != changes:
        raise unproven()
    if git_prefix_transaction_epoch(database) != epoch:
        raise unproven()
    window = GitPrefixWriteWindow(database, catalog, rows, anchor, epoch)
    object.__setattr__(window, "_witness", ref(window))
    _register_git_prefix_write_window(database, window)
    return window


def _unchanged_history(
    before: GitPrefixRows, after: GitPrefixRows, checkpoint: Callable[[], None]
) -> None:
    """只允许追加新事件及它们的当前投影，不允许改写原事件、Seal或索引。"""
    old = record_bodies(before, checkpoint=checkpoint)
    new = record_bodies(after, checkpoint=checkpoint)
    if any(new.get(key) != body for key, body in old.items()):
        raise unproven()
    immutable = ("git_record_publications", "git_native_bridge_index")
    for name in immutable:
        checkpoint()
        observed = set(after.table(name))
        if not set(before.table(name)) <= observed:
            raise unproven()
    # 尚未接入原生桥接解释器之前，不允许该有限发布原语认证新的桥接索引。
    if before.table("git_native_bridge_index") != after.table("git_native_bridge_index"):
        raise KernelError("git_native_bridge_unavailable", "Git原生桥接写端尚未装配")
    # 原完整事件payload不变之外，各旧事件的冗余身份、phase列也必须不变。
    for name in (
        "git_product_link_events",
        "git_object_inventory_events",
        "git_worktree_events",
        "git_commit_events",
        "git_checkpoints",
    ):
        if not set(before.table(name)) <= set(after.table(name)):
            raise unproven()
    if before.table("git_record_publications") != after.table("git_record_publications"):
        raise unproven()
    _immutable_projections(before, after)


def _immutable_projections(before: GitPrefixRows, after: GitPrefixRows) -> None:
    """新事件只能推进原投影，不能趁其他实体发布时改写旧身份或计划绑定。"""
    layouts = (
        ("git_product_links", (0, 1, 2, 3, 4, 5, 6, 7), 9),
        ("git_object_inventories", (0, 1, 2, 6), 4),
        ("git_worktrees", (0, 1, 2), 4),
        ("git_commits", (0, 1, 2, 3), 5),
    )
    for table, indices, sequence_column in layouts:
        current = {row[0]: row for row in after.table(table)}
        for row in before.table(table):
            updated = current.get(row[0])
            if updated is None or any(row[index] != updated[index] for index in indices):
                raise unproven()
            if row[sequence_column] == updated[sequence_column] and row != updated:
                raise unproven()


def _window_rows(window: GitPrefixWriteWindow, checkpoint: Callable[[], None]) -> GitPrefixRows:
    if (
        type(window) is not GitPrefixWriteWindow
        or type(window._witness) is not ReferenceType
        or window._witness() is not window
        or window._used
    ):
        raise unproven()
    _writable_transaction(window.database)
    _require_issued_git_prefix_window(window.database, window)
    if git_prefix_transaction_epoch(window.database) != window.transaction_epoch:
        raise unproven()
    if window.database.execute("SELECT * FROM git_prefix_anchor").fetchone() != window.anchor:
        raise unproven()
    rows = capture_git_prefix_rows(window.database, checkpoint=checkpoint)
    _unchanged_history(window.rows, rows, checkpoint)
    return rows


def _publication_row(
    claims: GitDeliveryRecordClaims, body: bytes, seal: bytes
) -> tuple[object, ...]:
    return (
        claims.record_kind,
        str(claims.record_id),
        str(claims.publication_epoch),
        claims.sequence,
        str(claims.delivery_id),
        str(claims.thread_id),
        str(claims.turn_id),
        str(claims.call_id),
        str(claims.route_id),
        claims.previous_sha256,
        sha256(body).hexdigest(),
        len(body),
        extend_prefix(claims.previous_sha256, seal),
        seal,
    )


async def publish_git_prefix_changes(
    window: GitPrefixWriteWindow,
    additions: tuple[GitDeliveryRecordClaims, ...],
    authority: GitPublicationAuthority,
    prefix_authority: GitStorePrefixAuthority,
    protection: PublicOutputProtection,
    *,
    cancel: CancelToken,
) -> GitPrefixCatalog:
    """认证本窗口有限新事实并写尾锚；调用者必须失败回滚、成功自行COMMIT。"""
    cancel.checkpoint()
    rows = _window_rows(window, cancel.checkpoint)
    if type(additions) is not tuple or not additions:
        raise unproven()
    frozen = tuple(snapshot_git_delivery_claims(c) for c in additions)
    old = record_bodies(window.rows, checkpoint=cancel.checkpoint)
    new = record_bodies(rows, checkpoint=cancel.checkpoint)
    wanted = {(c.record_kind, str(c.record_id), c.sequence - 1) for c in frozen}
    if len(wanted) != len(frozen) or wanted != new.keys() - old.keys():
        raise unproven()
    if (
        authority.identity() != (window.catalog.store_id, window.catalog.key_id)
        or authority.identity() != prefix_authority.identity()
    ):
        raise unproven()
    object.__setattr__(window, "_used", True)
    publications = []
    for claims in frozen:
        body = new[claims.record_kind, str(claims.record_id), claims.sequence - 1]
        seal = await authority.issue(claims, body, protection, cancel=cancel)
        publications.append(_publication_row(claims, body, seal))
    if capture_git_prefix_rows(window.database, checkpoint=cancel.checkpoint) != rows:
        raise unproven()
    window.database.executemany(
        "INSERT INTO git_record_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", publications
    )
    published = capture_git_prefix_rows(window.database, checkpoint=cancel.checkpoint)
    catalog = build_git_prefix_catalog(
        published, authority, window.catalog.genesis_epoch, checkpoint=cancel.checkpoint
    )
    seal = await prefix_authority.issue(
        GitStorePrefixAnchorClaims(
            schema_version="2", store_genesis_epoch=catalog.genesis_epoch, revision=catalog.revision
        ),
        encode_git_prefix_catalog(catalog),
        protection,
        cancel=cancel,
    )
    _writable_transaction(window.database)
    if git_prefix_transaction_epoch(window.database) != window.transaction_epoch:
        raise unproven()
    if (
        capture_git_prefix_rows(window.database, checkpoint=cancel.checkpoint) != published
        or window.database.execute("SELECT * FROM git_prefix_anchor").fetchone() != window.anchor
    ):
        raise unproven()
    if window.database.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise unproven()
    _save_anchor(window.database, catalog, seal)
    cancel.checkpoint()
    read_git_prefix_catalog(
        window.database, authority, prefix_authority, checkpoint=cancel.checkpoint
    )
    return catalog
