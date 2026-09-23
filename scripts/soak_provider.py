"""为本地长会话Soak提供不保留请求正文的确定性模型夹具。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
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


class SoakSummaryProvider:
    """仅为真实Compaction提供确定性摘要和完整用量账本，不保留请求正文。"""

    __slots__ = ("request_count",)

    RESPONSE_TEXT = "保留已完成的工程约束与恢复事实。"

    def __init__(self) -> None:
        self.request_count = 0

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.request_count += 1
        started = ModelAttemptStarted(
            attempt_id=uuid4(),
            step=request.step,
            index=1,
            provider="soak_summary",
            requested_model="soak-summary-v1",
        )
        del request
        cancel.checkpoint()
        yield started
        cancel.checkpoint()
        yield ResponseStarted(response_id="soak-summary")
        cancel.checkpoint()
        yield TextStarted(content_id="soak-summary-text")
        cancel.checkpoint()
        yield TextDelta(content_id="soak-summary-text", delta=self.RESPONSE_TEXT)
        cancel.checkpoint()
        yield ModelUsageObserved(
            attempt_id=started.attempt_id,
            actual_model="soak-summary-v1",
            response_id="soak-summary",
            usage=UsageObservation(completeness="complete", input_tokens=10, output_tokens=3),
        )
        cancel.checkpoint()
        yield ModelAttemptFinished(attempt_id=started.attempt_id, outcome="completed")
        cancel.checkpoint()
        yield TextCompleted(content_id="soak-summary-text", text=self.RESPONSE_TEXT)
        cancel.checkpoint()
        yield ResponseCompleted(usage=Usage(input_tokens=10, output_tokens=3))
