"""Artifact表写入细节：集中维护列顺序和新增元数据。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import aiosqlite

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, ArtifactRef
from harnessix.artifacts.publication import ArtifactPublicationGuard

# SQLite substr在返回Python前截断受攻击者控制的正文、Manifest和Seal；上限+1使超限仍可检测。
ARTIFACT_READ_SELECT = (
    "SELECT artifact_id,thread_id,turn_id,call_id,workspace_scope,"
    "substr(manifest_json,1,8193) AS manifest_json,size_bytes,expires_at,state,"
    f"CASE WHEN body=x'' THEN x'' ELSE substr(body,1,{MAX_ARTIFACT_BYTES + 1}) "
    "END AS body,purpose,created_at,"
    "publication_epoch,publication_policy,"
    "substr(publication_seal,1,4097) AS publication_seal FROM agent_artifacts"
)


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
    publication: ArtifactPublicationGuard | None = None,
) -> None:
    """使用显式列名写入正文与Manifest，避免Migration扩列破坏调用方。"""

    if publication is not None:
        await publication.check_body(body, purpose=purpose)
    await database.execute(
        "INSERT INTO agent_artifacts "
        "(artifact_id,thread_id,turn_id,call_id,workspace_scope,manifest_json,size_bytes,"
        "expires_at,state,body,purpose,created_at,publication_epoch,publication_policy) "
        "VALUES (?,?,?,?,?,?,?,?,'published',?,?,?,?,?)",
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
            publication.epoch if publication is not None else None,
            publication.policy if publication is not None else None,
        ),
    )
    if publication is not None and publication.binding is not None:
        cursor = await database.execute(
            "SELECT * FROM agent_artifacts WHERE artifact_id = ?", (str(ref.artifact_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Artifact插入后无法读取原行")
        seal = publication.issue_proof(row)
        await database.execute(
            "UPDATE agent_artifacts SET publication_seal = ? WHERE artifact_id = ?",
            (seal, str(ref.artifact_id)),
        )
