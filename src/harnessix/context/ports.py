from typing import Protocol

from harnessix.context.contracts import ContextBuildInput, PreparedContext


class ContextPlanner(Protocol):
    def prepare(self, request: ContextBuildInput) -> PreparedContext: ...
