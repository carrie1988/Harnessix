from typing import Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.domain.models import ToolDescriptor


class ToolRuntime(Protocol):
    def definitions(self) -> tuple[ToolDescriptor, ...]: ...

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent: ...


class NoTools:
    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return ()

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        return ToolResultContent(call_id=call.call_id, outcome="failed")
