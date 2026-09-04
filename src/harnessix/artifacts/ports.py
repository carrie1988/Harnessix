from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from harnessix.agent.models import EventDraft, Thread, ToolCallContent
from harnessix.agent.ports import PatchBatchRuntime
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.session.ports import SessionStore


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
