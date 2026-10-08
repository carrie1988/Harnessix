"""只读核验原Schema、Session/Event/Artifact来源；不注册、迁移、重签或执行效果。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
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

# 冻结已发布的完整v30/v31迁移集合，不能用当前资源或任意旧前缀替代原合同。
_SESSION_MIGRATION_CHECKSUMS = (
    "f511dc6c2c86db4995b8a68b7812d8e6072aa82a1f19b0a559e0975a9e7d2c95",
    "043a8da8a557c8eaf18562d0e1679eb7fb56d1203b54cf866fb4d6684e6a0a33",
    "3c9b395782a2fc000382dd6cdfb2f215c9b04b96b7d0b2a0ab06657af9813b04",
    "301f45039593a6a75237ee1c5e7a0185a71d3603cac589b7b815af1410bfc04d",
    "1ef506e410816cb889d50957c7860a0907635d473d790ebcc632a959d4fd8a85",
    "b9189c639e65e67c93b0bfc5e8158ef4acf9b3392da2435d781aa36f97b431a0",
    "251f0b9b7c87413cab9488cd751541eead40d3a5d76279e9095ebd61fd57b847",
    "a6b6733ad430adf1bc2a41b69492d6376b551278ff75b2ac6c1dc95c2443c6ca",
    "67f19391a613b5525a5404732cc2a2429337fb8a8c5ae10f035c4ee98c5227a7",
    "fbcda6a8f05001fb1834aae2c75ed8e96d052632c8627777b62dabd5edb5b3fa",
    "12295e83c718c367ae0da730ea39395663728752d33cc24b620d3ee5c70104e2",
    "bb8961f08bc15171001340df72140984aec2889b279243c08be34ef4ae04f918",
    "e1e5af67ccd809c8587b24ae962c22bf9eb18b2eb81ae13617fc1f74395c8b09",
    "42d2530f4bc718c3cfea3ede22c220d04e7aafb0990e2404039cde87ed8a6157",
    "304f1bf9e5c0170a9a3703c11d655ef8aae98438884ab80d2e4f098b6db22835",
    "5a1babc80cc700c9f372d61ecdcb4457ed6b9552267bcc24fe61b1cddd1f9fbb",
    "d6bdd00f06924d02580129a96e785e000e75e748d0195b64345cefb317b19b02",
    "4cbe8c146e4ed71f021e9be45b63da3b5b691115307412dd61e4ffcb277a2f9c",
    "926e3bbb1ee98971815166b9737032b8bc63ace9d6fb84bc887380606d654c7a",
    "a431bc67ae1ee6d41e615f338af94c17021042e335c147ef520757ec289c6a1b",
    "2f9b2447235b7aa22cf5860a1d1765f84a437de6037392f663fd56ee4687bf82",
    "63e4fa0983de87e2d6bc5c8e4a5bbc126c6de6351c0abec99aacacacc808a0b1",
    "e84af4b8debbe614e134822e3c2c59d3809c5e80e257dc1baae1c6a8a6fd8a42",
    "e27386cbfd7e1d4d9fd0f20eb01e893b4d11be0ed398e859a0a4017e97618a46",
    "29c9b787d35c9cf9c339b99195e3c751f652c18af12ac748d13f84b2b43fbdfa",
    "1ebdbb4cdc20d69c9ea4be5ce8073ae0d64776f36ae6f6ee5b197a743ffa4b35",
    "893108bb74049829535e5b31629a826c23966d0b34558a75b016e5ff8ee0eefb",
    "d5dc14568d8ba6dfdcc94a060a17fc5b9c6929d12cba33a562a8ccd5d25d0098",
    "4aab60066b1bde339ac5964f51b21a5bfa56469f57d2fff99dadf665b193fe2e",
    "49cfb3045930284211e88d1f3210ecf6a54d686d8bae43e822c8d7375d5075eb",
    "0c4203f385ceb42ceb191e0f847cd5fa3525e7ac5c33eb99870fd3defd72d380",
)


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
    rows = database.execute("SELECT version,checksum FROM agent_migrations").fetchall()
    if len(rows) not in (30, 31) or any(
        type(version) is not int or type(checksum) is not str for version, checksum in rows
    ):
        raise invalid_state()
    applied = dict(rows)
    if len(applied) != len(rows) or applied != dict(
        enumerate(_SESSION_MIGRATION_CHECKSUMS[: len(rows)], 1)
    ):
        raise invalid_state()
    if len(applied) == 30 and (
        database.execute(
            "SELECT 1 FROM agent_threads WHERE projection_version > 20 LIMIT 1"
        ).fetchone()
        is not None
        or database.execute(
            "SELECT 1 FROM agent_events "
            "WHERE json_extract(event_json,'$.schema_version') > 20 LIMIT 1"
        ).fetchone()
        is not None
    ):
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
