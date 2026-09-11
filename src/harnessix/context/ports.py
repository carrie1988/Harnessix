"""模型Context规划：定义依赖倒置端口，不提供具体基础设施实现。"""

from typing import Protocol

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import ContextBuildInput, PreparedContext


class ContextPlanner(Protocol):
    """同步Context规划端口。"""

    def prepare(self, request: ContextBuildInput) -> PreparedContext: ...


class AsyncContextPlanner(Protocol):
    """异步Context规划端口。"""

    async def prepare(self, request: ContextBuildInput, cancel: CancelToken) -> PreparedContext: ...
