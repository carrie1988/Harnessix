from typing import Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import ContextBuildInput, PreparedContext


class ContextPlanner(Protocol):
    def prepare(self, request: ContextBuildInput) -> PreparedContext: ...


class AsyncContextPlanner(Protocol):
    async def prepare(self, request: ContextBuildInput, cancel: CancelToken) -> PreparedContext: ...
