"""只读核验原Schema、Session/Event/Artifact来源；不注册、迁移、重签或执行效果。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import aiosqlite
from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.artifacts.persistence import ARTIFACT_READ_SELECT
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.artifacts.reference import validate_artifact_reference
from harnessix.artifacts.sqlite import records
from harnessix.product_config.session_key_codec import (
    MAX_KEY_FILE_BYTES,
    POSIX_MAGIC,
    OwnedSessionKey,
    decode_payload,
)
from harnessix.product_config.state_backup_contracts import (
    DATABASES,
    KEY_FILE,
    MAX_STATE_PAYLOAD_BYTES,
    MAX_STATE_ROWS,
    PROCESS_DATABASE,
)
from harnessix.product_config.state_backup_files import PrivateStateTree, read_small
from harnessix.product_config.state_backup_records import validate_state_records
from harnessix.session.capacity import _protocol_capacity, load_thread_facts
from harnessix.session.maintenance_io import MaintenanceIOControl
from harnessix.session.sqlite_publication import authenticated_events
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.sqlite_readonly import readonly_database


def invalid_state() -> KernelError:
    return KernelError("product_backup_state_invalid", "产品备份Schema、原来源或跨Store事实不完整")


class _VerificationOnlyScope:
    """仅满足原MAC Reader构造合同，拒绝新输出保护；调用方不得调用签发接口。"""

    def publication_context(self) -> dict[str, object]:
        return {"scope_id": str(UUID(int=0)), "bindings": []}

    def assert_public_json(self, value: JsonValue, *, checkpoint: Callable[[], None]) -> None:
        raise invalid_state()

    def assert_public_jsonl(self, body: bytes, *, checkpoint: Callable[[], None]) -> None:
        raise invalid_state()


def original_key(tree: PrivateStateTree) -> OwnedSessionKey:
    """不调用初始化Loader，因而缺Key或未完成Key发布均不会在候选中创建替代身份。"""
    body = read_small(tree, KEY_FILE, MAX_KEY_FILE_BYTES)
    if os.name == "posix":
        if not body.startswith(POSIX_MAGIC):
            raise invalid_state()
        return decode_payload(body[len(POSIX_MAGIC) :])
    if os.name == "nt":
        from harnessix.product_config.session_key_windows import _decode

        return decode_payload(_decode(body))
    raise invalid_state()


def _bounded_tables(database: sqlite3.Connection, control: MaintenanceIOControl) -> None:
    database.set_progress_handler(control.interrupt, 1000)
    if (
        database.execute("PRAGMA quick_check").fetchall() != [("ok",)]
        or database.execute("PRAGMA foreign_key_check").fetchone() is not None
    ):
        raise invalid_state()
    names = database.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall()
    if len(names) > 64:
        raise invalid_state()
    for (name,) in names:
        control.checkpoint()
        quoted = '"' + name.replace('"', '""') + '"'
        if database.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0] > MAX_STATE_ROWS:
            raise invalid_state()
        columns = {row[1] for row in database.execute(f"PRAGMA table_info({quoted})")}
        for column in ("payload", "snapshot_json", "event_json", "outcome_json"):
            if column in columns:
                size = database.execute(
                    f"SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {quoted}"
                ).fetchone()[0]
                if size > MAX_STATE_PAYLOAD_BYTES:
                    raise invalid_state()


def _session_schema(database: sqlite3.Connection) -> None:
    if database.execute("PRAGMA application_id").fetchone() != (0x4858534B,):
        raise invalid_state()
    expected = {
        int(resource.name.split("_", 1)[0]): hashlib.sha256(resource.read_bytes()).hexdigest()
        for resource in files("harnessix.session.migrations").iterdir()
        if resource.name.endswith(".sql")
    }
    if dict(database.execute("SELECT version,checksum FROM agent_migrations")) != expected:
        raise invalid_state()


def _schemas(root: Path, process: bool, control: MaintenanceIOControl) -> None:
    metadata = {
        "action-audit.db": ("action_audit_metadata", ("2", "3")),
        "execution-plans.db": ("execution_store_metadata", ("1", "2")),
        "product-config.db": ("product_config_metadata", ("1",)),
        "workspace-transactions/transactions.db": ("delivery_metadata", ("1", "2", "3")),
        PROCESS_DATABASE: ("process_store_metadata", ("2",)),
    }
    for path in (*DATABASES, *((PROCESS_DATABASE,) if process else ())):
        with closing(readonly_database(root / path)) as database:
            _bounded_tables(database, control)
            if path == "sessions.db":
                _session_schema(database)
            elif path in metadata:
                table, versions = metadata[path]
                row = database.execute(
                    f"SELECT value FROM {table} WHERE key='schema_version'"
                ).fetchone()
                if row is None or row[0] not in versions:
                    raise invalid_state()


async def _session_facts(
    root: Path,
    binding: SessionPublicationBinding,
    control: MaintenanceIOControl,
) -> tuple[Thread, ...]:
    database = await aiosqlite.connect((root / "sessions.db").as_uri() + "?mode=ro", uri=True)
    try:
        database.row_factory = aiosqlite.Row
        await database.set_progress_handler(control.interrupt, 1000)
        await database.execute("PRAGMA query_only=ON")
        await database.execute("BEGIN")
        facts = await load_thread_facts(database, binding)
        threads = {str(fact.thread.thread_id): fact.thread for fact in facts}
        for fact in facts:
            control.checkpoint()
            await authenticated_events(database, binding, fact.thread.thread_id, 0)
        await _protocol_capacity(database)
        guard = ArtifactPublicationGuard(_VerificationOnlyScope(), binding)
        cursor = await database.execute(ARTIFACT_READ_SELECT + " ORDER BY artifact_id")
        async for row in cursor:
            control.checkpoint()
            thread = threads.get(row["thread_id"])
            if thread is None:
                raise invalid_state()
            reference = validate_artifact_reference(row, thread)
            if row["state"] == "published":
                guard.require_proof(row)
                if (
                    reference.sha256 != hashlib.sha256(row["body"]).hexdigest()
                    or len(records(row["body"])) != reference.records
                ):
                    raise invalid_state()
            elif row["state"] != "expired" or row["body"] is not None:
                raise invalid_state()
        return tuple(threads.values())
    finally:
        await database.close()


def validate_product_state(
    tree: PrivateStateTree,
    paths: tuple[str, ...],
    control: MaintenanceIOControl,
) -> tuple[UUID, UUID]:
    """复制完成后核验所有原事实；成功不授予公开读取或高风险效果重放权限。"""
    process = PROCESS_DATABASE in paths
    _schemas(tree.path, process, control)
    material = original_key(tree)
    binding = SessionPublicationBinding(
        material.store_id, material.key_id, bytes(material.key), _VerificationOnlyScope()
    )
    try:
        threads = asyncio.run(_session_facts(tree.path, binding, control))
        validate_state_records(tree, threads, paths, control)
        control.checkpoint()
        return material.store_id, material.key_id
    finally:
        binding.close()
        material.close()


async def _workspaces(
    tree: PrivateStateTree, binding: SessionPublicationBinding, control: MaintenanceIOControl
) -> tuple[str, ...]:
    database = await aiosqlite.connect((tree.path / "sessions.db").as_uri() + "?mode=ro", uri=True)
    try:
        database.row_factory = aiosqlite.Row
        await database.set_progress_handler(control.interrupt, 1000)
        await database.execute("PRAGMA query_only=ON")
        await database.execute("BEGIN")
        facts = await load_thread_facts(database, binding)
        return tuple(fact.thread.workspace for fact in facts)
    finally:
        await database.close()


def require_backup_outside_workspaces(
    tree: PrivateStateTree, destination: Path, control: MaintenanceIOControl
) -> None:
    """复制Key之前，从原认证投影核对目标，避免把私有备份写入用户Git工作区。"""
    with closing(original_key(tree)) as material:
        binding = SessionPublicationBinding(
            material.store_id, material.key_id, bytes(material.key), _VerificationOnlyScope()
        )
        try:
            for workspace in asyncio.run(_workspaces(tree, binding, control)):
                control.checkpoint()
                root = Path(workspace).resolve(strict=False)
                if destination.is_relative_to(root) or root.is_relative_to(destination):
                    raise KernelError(
                        "product_backup_workspace_overlap", "备份目录不能与受管Workspace重叠"
                    )
        finally:
            binding.close()
