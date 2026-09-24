"""模型历史Artifact引用的批量验证：共享连接与快照，逐引用语义与单条路径一致。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

import aiosqlite

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.artifacts.contracts import (
    ArtifactOmittedField,
    ArtifactRef,
    HistoryArtifactPurpose,
    HistoryReferenceCheck,
)
from harnessix.artifacts.ports import ArtifactAccessScope, ArtifactReferenceVerifier
from harnessix.artifacts.sqlite import _HISTORY_ARTIFACT_PURPOSES, SQLiteArtifactStore

_ROW_CHUNK = 100


class _ReferenceBinding(Protocol):
    """历史引用绑定的最小结构；避免artifacts反向依赖context模块。"""

    @property
    def artifact(self) -> ArtifactRef: ...

    @property
    def purpose(self) -> HistoryArtifactPurpose: ...


class _PreparedReference(Protocol):
    """context准备的单条历史引用的最小结构。"""

    @property
    def owner_thread_id(self) -> UUID | None: ...

    @property
    def call_id(self) -> UUID: ...

    @property
    def binding(self) -> _ReferenceBinding: ...

    @property
    def omitted_field(self) -> ArtifactOmittedField | None: ...


async def _fetch_rows(
    database: aiosqlite.Connection,
    entries: Sequence[HistoryReferenceCheck],
    workspace_scope: str,
) -> dict[str, aiosqlite.Row]:
    """按分批IN查询取回全部候选归属行，调用方负责事务边界。"""

    rows: dict[str, aiosqlite.Row] = {}
    identities = sorted({str(entry.reference.artifact_id) for entry in entries})
    for start in range(0, len(identities), _ROW_CHUNK):
        chunk = identities[start : start + _ROW_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        cursor = await database.execute(
            f"SELECT * FROM agent_artifacts WHERE artifact_id IN ({marks}) "  # noqa: S608
            "AND workspace_scope = ?",
            (*chunk, workspace_scope),
        )
        for row in await cursor.fetchall():
            rows[row["artifact_id"]] = row
    return rows


def _check_entry(
    store: SQLiteArtifactStore,
    rows: dict[str, aiosqlite.Row],
    snapshots: dict[UUID, Thread | None],
    entry: HistoryReferenceCheck,
) -> None:
    """按单条路径的精确顺序与错误码检查一条引用。"""

    record = rows.get(str(entry.reference.artifact_id))
    if record is None or record["thread_id"] != str(entry.owner_thread_id):
        raise KernelError("artifact_not_found", "Artifact不存在或不属于当前作用域")
    if entry.purpose != "artifact_page" and (
        record["call_id"] != str(entry.call_id) or record["purpose"] != entry.purpose
    ):
        raise KernelError("artifact_not_found", "Artifact不存在或不属于当前作用域")
    thread = snapshots[entry.owner_thread_id]
    if thread is None:
        raise KernelError("artifact_corrupt", "Artifact归属不存在")
    try:
        stored = store._reference(record, thread)  # noqa: SLF001
    except KernelError as error:
        if error.code == "artifact_unreferenced":
            raise KernelError("artifact_not_found", "Artifact不存在或不属于当前作用域") from None
        raise
    if stored != entry.reference:
        raise KernelError("artifact_corrupt", "Artifact引用与已提交manifest不一致")
    lines = store._body(record, thread, stored)  # noqa: SLF001
    if entry.purpose == "artifact_page":
        store._verify_page(thread, entry.call_id, stored, lines)  # noqa: SLF001
    if entry.omitted_field is not None:
        if entry.purpose != "tool_result" or not stored.complete:
            raise KernelError("artifact_corrupt", "局部Artifact不能证明结果省略")
        store._verify_coverage(thread, entry.call_id, lines, entry.omitted_field)  # noqa: SLF001


async def verify_references(
    store: SQLiteArtifactStore,
    entries: Sequence[HistoryReferenceCheck],
    *,
    workspace_scope: str,
) -> None:
    """一次连接与每归属一次快照完成同步骤全部历史引用验证。"""

    if not entries:
        return
    for entry in entries:
        if entry.purpose not in _HISTORY_ARTIFACT_PURPOSES:
            raise KernelError("artifact_invalid", "Artifact用途不符合契约")
    async with store.session._connection() as database:  # noqa: SLF001
        await database.execute("BEGIN")
        rows = await _fetch_rows(database, entries, workspace_scope)
        snapshots: dict[UUID, Thread | None] = {}
        for entry in entries:
            if entry.owner_thread_id not in snapshots:
                snapshots[entry.owner_thread_id] = await store.session._snapshot(  # noqa: SLF001
                    database, entry.owner_thread_id
                )
        for entry in entries:
            _check_entry(store, rows, snapshots, entry)


def _entry(reference: _PreparedReference, thread: Thread) -> HistoryReferenceCheck:
    """把context准备的历史引用转换为批量验证输入。"""

    return HistoryReferenceCheck(
        owner_thread_id=reference.owner_thread_id or thread.thread_id,
        call_id=reference.call_id,
        reference=reference.binding.artifact,
        purpose=reference.binding.purpose,
        omitted_field=reference.omitted_field,
    )


async def verify_history_references(
    verifier: ArtifactReferenceVerifier,
    access: ArtifactAccessScope,
    thread: Thread,
    references: Sequence[_PreparedReference],
    token: CancelToken,
) -> None:
    """验证当前步骤全部历史引用；SQLite存储走共享批量读取，其他Verifier回退逐条路径。"""

    scope = await token.run(access.artifact_workspace_scope(thread.workspace, token))
    if isinstance(verifier, SQLiteArtifactStore):
        entries = tuple(_entry(reference, thread) for reference in references)
        token.checkpoint()
        await token.run(verify_references(verifier, entries, workspace_scope=scope))
        token.checkpoint()
        return
    for reference in references:
        token.checkpoint()
        await token.run(
            verifier.verify_reference(
                reference.owner_thread_id or thread.thread_id,
                reference.call_id,
                reference.binding.artifact,
                workspace_scope=scope,
                purpose=reference.binding.purpose,
                omitted_field=reference.omitted_field,
            )
        )
        token.checkpoint()
