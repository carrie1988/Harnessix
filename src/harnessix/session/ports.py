from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from harnessix.agent.models import AgentEvent, EventDraft, Thread


class SessionStore(Protocol):
    async def initialize(self) -> None: ...

    def runtime_owner(self) -> AbstractAsyncContextManager[None]: ...

    async def get_thread(self, thread_id: UUID) -> Thread: ...

    async def thread_ids(self) -> list[UUID]: ...

    async def append(
        self,
        thread_id: UUID,
        drafts: Sequence[EventDraft],
        *,
        expected_sequence: int,
    ) -> Thread: ...

    async def events(self, thread_id: UUID, *, after: int = 0) -> list[AgentEvent]: ...

    async def rebuild(self, thread_id: UUID) -> Thread: ...
