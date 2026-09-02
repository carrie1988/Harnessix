from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
)


class ScriptedProvider:
    """按 Model Step 重放归一化事件；不发起网络请求，也不执行工具。"""

    def __init__(
        self, steps: Sequence[Sequence[ProviderEvent]], *, delay_seconds: float = 0
    ) -> None:
        self.steps = tuple(tuple(event.model_copy(deep=True) for event in step) for step in steps)
        self.delay_seconds = delay_seconds
        self.requests: list[ModelRequest] = []
        self.closed_streams = 0

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests.append(request.model_copy(deep=True))
        try:
            if request.step > len(self.steps):
                raise KernelError("script_exhausted", "Scripted Provider 没有对应模型步骤")
            for event in self.steps[request.step - 1]:
                cancel.checkpoint()
                if self.delay_seconds:
                    await asyncio.sleep(self.delay_seconds)
                yield event.model_copy(deep=True)
        finally:
            self.closed_streams += 1


class FakeProvider(ScriptedProvider):
    def __init__(self, text: str = "已完成") -> None:
        super().__init__(
            [
                [
                    ResponseStarted(response_id="fake-response"),
                    TextStarted(content_id="answer"),
                    TextDelta(content_id="answer", delta=text),
                    TextCompleted(content_id="answer", text=text),
                    ResponseCompleted(),
                ]
            ]
        )
