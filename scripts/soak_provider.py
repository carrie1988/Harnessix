"""为本地长会话Soak提供不保留请求正文的确定性模型夹具。"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from harnessix.agent.cancellation import CancelToken
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
)


class SoakProvider:
    """按固定脚本输出文本，只保留请求计数，不保留请求或响应正文。"""

    __slots__ = ("request_count",)

    SCRIPT_VERSION = "harnessix.soak-provider/v1"
    RESPONSE_TEXT = "soak-complete"

    def __init__(self) -> None:
        self.request_count = 0

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        """输出固定文本事件；每个事件提交前都检查取消令牌。"""
        del request
        self.request_count += 1

        cancel.checkpoint()
        yield ResponseStarted(response_id="soak-response")
        cancel.checkpoint()
        yield TextStarted(content_id="soak-answer")
        cancel.checkpoint()
        yield TextDelta(content_id="soak-answer", delta=self.RESPONSE_TEXT)
        cancel.checkpoint()
        yield TextCompleted(content_id="soak-answer", text=self.RESPONSE_TEXT)
        cancel.checkpoint()
        yield ResponseCompleted()
