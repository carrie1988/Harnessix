"""事务Artifact存储：定义依赖倒置端口，不提供具体基础设施实现。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from harnessix.agent.models import EventDraft, Thread, ToolCallContent
from harnessix.artifacts.contracts import (
    ArtifactOmittedField,
    ArtifactRef,
    ArtifactToolResult,
    HistoryArtifactPurpose,
)
from harnessix.session.ports import SessionStore

if TYPE_CHECKING:
    from harnessix.agent.cancellation import CancelToken
    from harnessix.agent.ports import PatchBatchRuntime, ProcessObservation, ProcessRuntime


@runtime_checkable
class ArtifactAccessScope(Protocol):
    async def artifact_workspace_scope(self, workspace: str, cancel: CancelToken) -> str: ...


class ArtifactReferenceVerifier(Protocol):
    @property
    def session(self) -> SessionStore: ...

    async def verify_reference(
        self,
        thread_id: UUID,
        call_id: UUID,
        reference: ArtifactRef,
        *,
        workspace_scope: str,
        purpose: HistoryArtifactPurpose,
        omitted_field: ArtifactOmittedField | None = None,
    ) -> None: ...


class ArtifactPublisher(ArtifactReferenceVerifier, Protocol):
    async def publish(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        output: ArtifactToolResult,
        *,
        expected_sequence: int,
        max_output_chars: int,
    ) -> Thread: ...


class BatchDiffPublisher(Protocol):
    @property
    def session(self) -> SessionStore: ...

    @property
    def bridge(self) -> PatchBatchRuntime: ...

    @property
    def artifacts(self) -> ArtifactReferenceVerifier: ...

    async def append(
        self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
    ) -> Thread: ...


class ProcessArtifactPublisher(Protocol):
    """将同一次终态观察的流正文与Session引用原子发布。"""

    @property
    def session(self) -> SessionStore: ...

    @property
    def bridge(self) -> ProcessRuntime: ...

    @property
    def artifacts(self) -> ArtifactReferenceVerifier: ...

    async def append(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        observation: ProcessObservation,
        drafts: Sequence[EventDraft],
        *,
        expected_sequence: int,
        max_output_chars: int,
    ) -> Thread: ...
