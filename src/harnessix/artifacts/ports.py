from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from harnessix.agent.models import EventDraft, Thread, ToolCallContent
from harnessix.agent.ports import PatchBatchRuntime
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.session.ports import SessionStore

if TYPE_CHECKING:
    from harnessix.agent.ports import ProcessObservation, ProcessRuntime


class ArtifactPublisher(Protocol):
    @property
    def session(self) -> SessionStore: ...

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

    async def append(
        self, thread_id: UUID, drafts: Sequence[EventDraft], *, expected_sequence: int
    ) -> Thread: ...


class ProcessArtifactPublisher(Protocol):
    """将同一次终态观察的流正文与Session引用原子发布。"""

    @property
    def session(self) -> SessionStore: ...

    @property
    def bridge(self) -> ProcessRuntime: ...

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
