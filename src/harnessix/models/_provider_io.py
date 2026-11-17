"""模型Provider适配：跟踪Provider输出暴露边界与资源关闭。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure
from harnessix.agent.usage import ModelAttemptFinished, ModelUsageObserved, UsageObservation
from harnessix.models._bounded_http import TRANSFER_ACTIVITY
from harnessix.models.config import ModelHTTPConfig
from harnessix.models.contracts import (
    ProviderEvent,
    ReasoningDelta,
    ResponseFailed,
    ResponseStarted,
    TextDelta,
    TextStarted,
    ToolArgumentsProgress,
)

MAX_PROVIDER_API_KEY_BYTES = 8 * 1024
MAX_RETRY_AFTER_SECONDS = 60.0


def unbilled_usage(attempt_id: UUID) -> ModelUsageObserved:
    """服务端明确拒绝或连接未建立的尝试没有消耗模型，记为完整的零用量。"""

    return ModelUsageObserved(
        attempt_id=attempt_id,
        usage=UsageObservation(completeness="complete", input_tokens=0, output_tokens=0),
    )


def retry_after_seconds(error: Exception) -> float:
    """读取错误响应的Retry-After秒数并限制上限；缺失或不是数字时返回0。"""

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    try:
        seconds: float = float(headers.get("retry-after")) if headers is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    return min(max(0.0, seconds), MAX_RETRY_AFTER_SECONDS)


def retry_delay_seconds(base: float, attempt: int, retry_after: float) -> float:
    """指数退避与服务端要求的等待时间取较大值。"""

    return max(base * float(2**attempt), retry_after)


def finish_attempt(attempt_id: UUID, failure: ResponseFailed | None = None) -> ModelAttemptFinished:
    return ModelAttemptFinished(
        attempt_id=attempt_id,
        outcome="failed" if failure else "completed",
        error=AgentFailure(
            code="provider_" + failure.code,
            message="Provider 返回结构化失败",
            retryable=failure.retryable,
        )
        if failure
        else None,
    )


async def wait_for_io[T](operation: Awaitable[T], cancel: CancelToken, deadline: float) -> T:
    # 每次 anext 可来自不同 Task，不能让 timeout 上下文跨越 yield。
    async with asyncio.timeout(max(0, deadline - asyncio.get_running_loop().time())):
        return await cancel.run(operation)


class StreamSilence:
    """一次流式尝试允许的静默（ADR 0166）。

    工具调用开始之前，连续io_timeout_seconds收不到任何字节按读取超时处理，与HTTP客户端读取期限
    原有的语义相同（可重试的传输失败），从请求交给传输层发送时开始计时。工具调用开始之后服务端
    仍在生成：有的服务只边生成边发送字符串参数，数组、对象等参数写完才整段发出（实测vLLM的Qwen
    工具解析器），期间连接上没有字节；此后静默只受本次请求的总期限约束。字节到达由传输层回调，
    这里不解析内容。
    """

    def __init__(
        self,
        idle_seconds: float,
        deadline: float,
        timeout: Callable[[], Exception],
        tool_call_started: Callable[[], bool],
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._idle = idle_seconds
        self._deadline = deadline
        self._timeout = timeout
        self._tool_call_started = tool_call_started
        # 请求交给传输层之前没有读取期限（与HTTP客户端相同），只受总期限约束。
        self._activity: float | None = None
        self._scope: asyncio.Timeout | None = None

    def received(self) -> None:
        """传输层收到一批字节（含SSE注释与心跳）。"""
        self._activity = self._loop.time()
        self._reschedule()

    def limit(self) -> float:
        if self._activity is None or self._tool_call_started():
            return self._deadline
        return min(self._deadline, self._activity + self._idle)

    async def wait[T](self, operation: Awaitable[T], cancel: CancelToken) -> T:
        """与wait_for_io相同的取消与总期限；静默超限时抛出适配器的读取超时。"""
        scope = asyncio.timeout(max(0.0, self.limit() - self._loop.time()))
        try:
            async with scope:
                self._scope = scope
                try:
                    return await cancel.run(self._attributed(operation))
                finally:
                    self._scope = None
        except TimeoutError:
            if scope.expired() and self._silent_too_long():
                raise self._timeout() from None
            raise

    async def chunks[T](
        self, stream: AsyncIterator[T], cancel: CancelToken
    ) -> AsyncGenerator[T, None]:
        """逐个取流式响应的分片；每次等待都受静默与总期限约束。"""
        while True:
            try:
                chunk = await self.wait(anext(stream), cancel)
            except StopAsyncIteration:
                return
            yield chunk

    async def _attributed[T](self, operation: Awaitable[T]) -> T:
        # 在执行这次IO的子Task里登记回调：请求交给传输层发送时开始计时，之后每批字节都算活动。
        TRANSFER_ACTIVITY.set(self.received)
        return await operation

    def _silent_too_long(self) -> bool:
        # 只有真正静默超过读取期限才改报读取超时；总期限到达仍按原来的期限超时处理。
        activity = self._activity
        return (
            activity is not None
            and not self._tool_call_started()
            and self._loop.time() >= activity + self._idle
        )

    def _reschedule(self) -> None:
        scope = self._scope
        if scope is not None and not scope.expired():
            scope.reschedule(self.limit())


def validate_key(key: str, *, headers_env: str) -> str:
    if (
        not key
        or len(key) > MAX_PROVIDER_API_KEY_BYTES
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise ValueError("Provider API Key未配置或格式无效")
    if os.environ.get(headers_env):
        raise ValueError("请移除 Provider 自定义环境 Header，避免跨端点认证污染")
    return key


def read_key(config: ModelHTTPConfig, *, headers_env: str) -> str:
    return validate_key(os.environ.get(config.api_key_env, ""), headers_env=headers_env)


# 断开前只出现过这些事件时，丢弃重来是安全的（ADR 0144）：
# - 响应开始、思考增量与工具参数进度只作界面的临时显示，不建Item、不进模型历史；
#   用量快照由尝试账本记账（第3版，负责人2026-11-13决定“先做A”）。
# - 正文开始与正文增量：正文Item到TextCompleted才写入内容，运行时把未完成的正文标为失败、
#   界面撤下这段正文，新尝试从头生成（第5版，负责人2026-11-15选B）。
# 两个适配器都只在完整回复后才交出正文终值、工具调用与终态。
_DISCARDABLE_EVENTS = (
    ModelUsageObserved,
    ResponseStarted,
    ReasoningDelta,
    ToolArgumentsProgress,
    TextStarted,
    TextDelta,
)


def persistent_output(event: ProviderEvent) -> bool:
    """这个事件会留下不能作废的结果（完整正文、工具调用、终态）；出现过之后请求断开不再重试。"""

    return not isinstance(event, _DISCARDABLE_EVENTS)
