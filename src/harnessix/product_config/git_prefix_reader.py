"""GitDB独立尾锚与全表全事件只读核验；不补签、修复或声称业务闭包完成。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from hashlib import sha256
from uuid import UUID

from harnessix.product_config.git_prefix_catalog import (
    GitPrefixCatalog,
    decode_git_prefix_catalog,
    encode_git_prefix_catalog,
)
from harnessix.product_config.git_prefix_records import unproven, verify_record_streams
from harnessix.product_config.git_prefix_rows import GitPrefixRows, capture_git_prefix_rows
from harnessix.product_config.git_prefix_sql import (
    git_prefix_transaction_epoch,
    require_git_prefix_sql_window,
)
from harnessix.session.git_prefix_contracts import GitStorePrefixAnchorClaims
from harnessix.session.git_prefix_publication import GitStorePrefixVerifier
from harnessix.session.store_publication import GitPublicationVerifier


def build_git_prefix_catalog(
    rows: GitPrefixRows,
    verifier: GitPublicationVerifier,
    genesis_epoch: UUID,
    *,
    checkpoint: Callable[[], None],
) -> GitPrefixCatalog:
    """候选全集来自已经逐条验MAC的事件，不允许只有未认证的SHA摘要。"""
    streams = verify_record_streams(rows, verifier, checkpoint=checkpoint)
    store_id, key_id = verifier.identity()
    return GitPrefixCatalog(
        store_id=store_id,
        key_id=key_id,
        genesis_epoch=genesis_epoch,
        revision=sum(s.count for s in streams),
        tables=rows.tables,
        streams=streams,
    )


def _anchor(database: sqlite3.Connection) -> tuple[GitStorePrefixAnchorClaims, bytes, bytes]:
    try:
        rows = database.execute("SELECT * FROM main.git_prefix_anchor").fetchall()
        if len(rows) != 1:
            raise unproven()
        row = rows[0]
        if row[0] != 1 or type(row[5]) is not str or type(row[6]) is not bytes:
            raise unproven()
        body = row[5].encode("utf-8")
        if row[3:5] != (len(body), sha256(body).hexdigest()):
            raise unproven()
        claims = GitStorePrefixAnchorClaims(
            schema_version="2", store_genesis_epoch=UUID(row[2]), revision=row[1]
        )
        if str(claims.store_genesis_epoch) != row[2]:
            raise unproven()
        return claims, body, row[6]
    except (ValueError, TypeError, UnicodeError):
        raise unproven() from None


def read_git_prefix_catalog(
    database: sqlite3.Connection,
    verifier: GitPublicationVerifier,
    prefix_verifier: GitStorePrefixVerifier,
    *,
    checkpoint: Callable[[], None],
) -> GitPrefixCatalog:
    """在原事务与宿主共同锁窗口内验证全集；历史认证不授予新执行权限。"""
    checkpoint()
    require_git_prefix_sql_window(database)
    epoch, changes = git_prefix_transaction_epoch(database), database.total_changes
    if not database.in_transaction:
        from harnessix.agent.errors import KernelError

        raise KernelError("git_delivery_store_transaction_required", "Git目录读取需要已有事务")
    from harnessix.delivery.git_store_schema_v2 import verify_git_store_v2_schema

    verify_git_store_v2_schema(database)
    claims, body, seal = _anchor(database)
    prefix_verifier.verify(seal, claims, body, checkpoint=checkpoint)
    actual = decode_git_prefix_catalog(body)
    if (
        actual.store_id,
        actual.key_id,
    ) != verifier.identity() or verifier.identity() != prefix_verifier.identity():
        raise unproven()
    if (actual.genesis_epoch, actual.revision) != (claims.store_genesis_epoch, claims.revision):
        raise unproven()
    rows = capture_git_prefix_rows(database, checkpoint=checkpoint)
    expected = build_git_prefix_catalog(
        rows, verifier, claims.store_genesis_epoch, checkpoint=checkpoint
    )
    if encode_git_prefix_catalog(expected) != body:
        raise unproven()
    checkpoint()
    if git_prefix_transaction_epoch(database) != epoch or database.total_changes != changes:
        raise unproven()
    verifier.identity()
    prefix_verifier.identity()
    return actual
