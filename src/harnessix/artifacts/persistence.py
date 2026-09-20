"""Artifact表写入细节：集中维护列顺序和新增元数据。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import aiosqlite

from harnessix.artifacts.contracts import ArtifactRef


async def insert_artifact(
    database: aiosqlite.Connection,
    ref: ArtifactRef,
    *,
    thread_id: UUID,
    turn_id: UUID,
    call_id: UUID,
    workspace_scope: str,
    body: bytes,
    purpose: str,
    created_at: datetime,
) -> None:
    """使用显式列名写入正文与Manifest，避免Migration扩列破坏调用方。"""

    await database.execute(
        "INSERT INTO agent_artifacts "
        "(artifact_id,thread_id,turn_id,call_id,workspace_scope,manifest_json,size_bytes,"
        "expires_at,state,body,purpose,created_at) VALUES (?,?,?,?,?,?,?,?,'published',?,?,?)",
        (
            str(ref.artifact_id),
            str(thread_id),
            str(turn_id),
            str(call_id),
            workspace_scope,
            ref.model_dump_json(),
            ref.size_bytes,
            ref.expires_at.isoformat(),
            body,
            purpose,
            created_at.isoformat(),
        ),
    )
