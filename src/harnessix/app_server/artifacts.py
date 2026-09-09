from __future__ import annotations

from typing import Protocol
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.artifacts.contracts import ArtifactPage
from harnessix.artifacts.ports import ArtifactAccessScope
from harnessix.protocol.contracts import ArtifactPageResult, PublicArtifactRef
from harnessix.session.ports import SessionStore


class ArtifactPageStore(Protocol):
    @property
    def session(self) -> SessionStore: ...

    async def read(
        self,
        thread_id: UUID,
        workspace_scope: str,
        artifact_id: UUID,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> ArtifactPage: ...


class ScopedProtocolArtifactReader:
    """把Session归属和当前Workspace能力绑定到公共Artifact分页。"""

    def __init__(
        self,
        session: SessionStore,
        artifacts: ArtifactPageStore,
        access: ArtifactAccessScope,
    ) -> None:
        if artifacts.session is not session:
            raise ValueError("Artifact Reader必须绑定App Server的同一Session")
        self.session = session
        self.artifacts = artifacts
        self.access = access

    async def read(
        self,
        thread_id: UUID,
        artifact_id: UUID,
        *,
        offset: int,
        limit: int,
    ) -> ArtifactPageResult:
        thread = await self.session.get_thread(thread_id)
        cancel = CancelToken()
        workspace_scope = await self.access.artifact_workspace_scope(thread.workspace, cancel)
        page = await self.artifacts.read(
            thread_id,
            workspace_scope,
            artifact_id,
            offset=offset,
            limit=limit,
        )
        return ArtifactPageResult(
            artifact=PublicArtifactRef.model_validate(page.artifact.model_dump()),
            offset=page.offset,
            text=page.text,
            next_offset=page.next_offset,
        )
