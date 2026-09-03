from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.config import ModelHTTPConfig
from harnessix.models.contracts import ResponseFailed


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


def read_key(config: ModelHTTPConfig, *, headers_env: str) -> str:
    key = os.environ.get(config.api_key_env, "")
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("Provider API Key 环境变量未配置或格式无效")
    if os.environ.get(headers_env):
        raise ValueError("请移除 Provider 自定义环境 Header，避免跨端点认证污染")
    return key
