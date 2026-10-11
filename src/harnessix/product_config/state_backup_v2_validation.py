"""Backup v2候选GitDB只读验真；不迁移、补签、恢复或开放Writer。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_business_snapshot import build_git_delivery_business_snapshot
from harnessix.product_config.git_business_snapshot_wire import (
    decode_git_delivery_business_snapshot,
    encode_git_delivery_business_snapshot,
)
from harnessix.product_config.git_prefix_catalog import (
    GitPrefixCatalog,
    encode_git_prefix_catalog,
)
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog_and_rows
from harnessix.product_config.git_prefix_rows import GitPrefixRows
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.state_backup_contracts import MAX_STATE_FILE_BYTES
from harnessix.product_config.state_backup_files import PrivateStateTree, read_small
from harnessix.product_config.state_backup_v2_contracts import (
    GIT_BUSINESS_SNAPSHOT,
    GIT_DATABASE,
    ProductStateGitBackupProfile,
)
from harnessix.product_config.state_backup_validation import (
    invalid_state,
    verification_publication_binding,
)
from harnessix.session.maintenance_io import MaintenanceIOControl
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.sqlite_readonly import readonly_database

_SQLITE_HEADER_BYTES = 20
_SQLITE_MAGIC = b"SQLite format 3\0"
_ROLLBACK_JOURNAL_HEADER = b"\x01\x01"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _require_delete_journal_candidate(
    tree: PrivateStateTree, control: MaintenanceIOControl
) -> None:
    """在SQLite打开前拒绝WAL格式，避免只读打开为WAL候选创建共享内存边车。"""
    control.checkpoint()
    if any(
        os.path.lexists(tree.path / (GIT_DATABASE + suffix))
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    ):
        raise invalid_state()
    with tree.open_file(GIT_DATABASE) as descriptor:
        header = os.read(descriptor, _SQLITE_HEADER_BYTES)
    if header[: len(_SQLITE_MAGIC)] != _SQLITE_MAGIC or header[18:20] != _ROLLBACK_JOURNAL_HEADER:
        raise invalid_state()
    control.checkpoint()


def _read_authenticated_catalog(
    tree: PrivateStateTree,
    binding: SessionPublicationBinding,
    control: MaintenanceIOControl,
) -> tuple[GitPrefixCatalog, GitPrefixRows]:
    try:
        with closing(readonly_database(tree.path / GIT_DATABASE)) as database:
            with git_prefix_sql_window(database, checkpoint=control.checkpoint):
                database.execute("BEGIN")
                if (
                    database.execute("PRAGMA main.journal_mode").fetchone() != ("delete",)
                    or database.execute("PRAGMA main.quick_check").fetchall() != [("ok",)]
                    or database.execute("PRAGMA main.foreign_key_check").fetchone() is not None
                ):
                    raise invalid_state()
                return read_git_prefix_catalog_and_rows(
                    database,
                    binding.git_verifier,
                    binding.git_prefix_verifier,
                    checkpoint=control.checkpoint,
                )
    except sqlite3.Error:
        control.checkpoint()
        raise invalid_state() from None


def validate_git_backup_profile(
    tree: PrivateStateTree,
    profile: ProductStateGitBackupProfile,
    store_id: UUID,
    key_id: UUID,
    control: MaintenanceIOControl,
) -> None:
    """核对候选GitDB的原身份、完整认证前缀及Manifest剖面，不产生SQLite写入。"""
    _require_delete_journal_candidate(tree, control)
    with verification_publication_binding(tree) as binding:
        if binding.git_verifier.identity() != (store_id, key_id):
            raise invalid_state()
        catalog, rows = _read_authenticated_catalog(tree, binding, control)
        if (
            (catalog.store_id, catalog.key_id) != (store_id, key_id)
            or catalog.genesis_epoch != profile.store_genesis_epoch
            or catalog.revision != profile.prefix_revision
            or hashlib.sha256(encode_git_prefix_catalog(catalog)).hexdigest()
            != profile.prefix_anchor_sha256
        ):
            raise invalid_state()
        try:
            body = read_small(tree, GIT_BUSINESS_SNAPSHOT, MAX_STATE_FILE_BYTES)
        except (KernelError, OSError, ValueError):
            raise invalid_state() from None
        if hashlib.sha256(body).hexdigest() != profile.business_snapshot_sha256:
            raise invalid_state()
        try:
            actual = decode_git_delivery_business_snapshot(body, checkpoint=control.checkpoint)
            expected = build_git_delivery_business_snapshot(
                profile.capture_id, catalog, rows, checkpoint=control.checkpoint
            )
            if (
                actual.capture_id != profile.capture_id
                or actual.prefix != catalog
                or encode_git_delivery_business_snapshot(expected, checkpoint=control.checkpoint)
                != body
            ):
                raise invalid_state()
        except KernelError as error:
            if error.code == "git_business_snapshot_scope_unsupported":
                raise
            if error.code in {
                "git_business_snapshot_invalid",
                "git_decision_link_history_changed",
                "git_delivery_plan_invalid",
                "publication_history_unproven",
            }:
                raise invalid_state() from None
            raise
        try:
            if read_small(tree, GIT_BUSINESS_SNAPSHOT, MAX_STATE_FILE_BYTES) != body:
                raise invalid_state()
        except (KernelError, OSError, ValueError):
            raise invalid_state() from None
        control.checkpoint()
    _require_delete_journal_candidate(tree, control)
