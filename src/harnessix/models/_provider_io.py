"""模型Provider适配：跟踪Provider输出暴露边界与资源关闭。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure
from harnessix.agent.usage import ModelAttemptFinished, ModelUsageObserved, UsageObservation
from harnessix.models.config import ModelHTTPConfig
from harnessix.models.contracts import (
    ProviderEvent,
    ReasoningDelta,
    ResponseFailed,
    ResponseStarted,
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


# 不建Item、不进模型历史的事件：响应开始、思考增量与工具参数进度只作界面的临时显示，
# 用量快照由尝试账本记账。
# 断开前只出现过这些事件时，丢弃重来是安全的（负责人2026-11-13决定“先做A”）。
_TRANSIENT_EVENTS = (ModelUsageObserved, ResponseStarted, ReasoningDelta, ToolArgumentsProgress)


def persistent_output(event: ProviderEvent) -> bool:
    """这个事件会留下持久的结果（正文、工具调用、终态）；出现过这类事件之后请求断开不再重试。"""

    return not isinstance(event, _TRANSIENT_EVENTS)
