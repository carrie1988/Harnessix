from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Self, cast

import httpx
from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.completion_create_params import CompletionCreateParamsStreaming

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.ids import new_id
from harnessix.agent.usage import ModelAttemptStarted, ModelUsageObserved
from harnessix.models._bounded_http import BoundedStream, BoundedTransport, InvalidWireData
from harnessix.models._chat_mapping import build_request
from harnessix.models._chat_stream import ChatStream, ContentRefused, validate_frame
from harnessix.models._history import InvalidModelRequest
from harnessix.models._provider_io import finish_attempt, read_key, wait_for_io
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest, ProviderEvent, ResponseFailed


def _failure(error: Exception) -> ResponseFailed:
    if isinstance(error, InvalidModelRequest):
        return ResponseFailed(code="invalid_request")
    if isinstance(error, ContentRefused):
        return ResponseFailed(code="content_policy")
    if isinstance(error, APIError):
        if isinstance(error.__cause__, InvalidWireData):
            return ResponseFailed(code="invalid_provider_output")
        if error.code in ("insufficient_quota", "quota_exceeded"):
            return ResponseFailed(code="quota")
        if error.code == "context_length_exceeded":
            return ResponseFailed(code="context_overflow")
        if error.code in ("content_policy_violation", "content_filter"):
            return ResponseFailed(code="content_policy")
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status in (401, 403):
            return ResponseFailed(code="authentication")
        if status == 429:
            return ResponseFailed(code="rate_limit", retryable=True)
        if status >= 500:
            return ResponseFailed(code="provider_internal", retryable=True)
        if status in (408, 409):
            return ResponseFailed(code="transport", retryable=True)
        return ResponseFailed(code="invalid_request")
    if isinstance(error, APIConnectionError | httpx.TransportError | TimeoutError):
        return ResponseFailed(code="transport", retryable=True)
    if isinstance(error, APIError):
        return ResponseFailed(code="provider_internal", retryable=True)
    return ResponseFailed(code="invalid_provider_output")


class OpenAIChatProvider:
    """拥有 HTTP Client 的 Chat Completions Adapter；通过环境引用读取认证。"""

    def __init__(
        self, config: OpenAIChatConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = OpenAIChatConfig.model_validate_json(config.model_dump_json())
        key = read_key(self.config, headers_env="OPENAI_CUSTOM_HEADERS")
        client = httpx.AsyncClient(
            transport=BoundedTransport(
                transport or httpx.AsyncHTTPTransport(trust_env=False),
                self.config,
                validate_frame=validate_frame,
            ),
            trust_env=False,
            follow_redirects=False,
            timeout=config.io_timeout_seconds,
        )
        self._client = AsyncOpenAI(
            api_key=key,
            admin_api_key="",
            organization="",
            project="",
            webhook_secret="",
            base_url=config.base_url,
            max_retries=0,
            http_client=client,
            timeout=config.io_timeout_seconds,
            default_headers={"Accept-Encoding": "identity"},
        )
        self._closed = False

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("Provider 已关闭")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        await self._client.close()

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        cancel.checkpoint()
        if self._closed:
            yield ResponseFailed(code="invalid_request")
            return
        try:
            body, names = build_request(request, self.config)
        except (ValueError, TypeError, OverflowError):
            yield ResponseFailed(code="invalid_request")
            return
        deadline = asyncio.get_running_loop().time() + self.config.timeout_seconds
        exposed = False
        for attempt in range(self.config.max_attempts):
            cancel.checkpoint()
            attempt_id = new_id()
            yield ModelAttemptStarted(
                attempt_id=attempt_id,
                step=request.step,
                index=attempt + 1,
                provider="openai_chat",
                requested_model=self.config.model,
            )
            stream: AsyncStream[ChatCompletionChunk] | None = None
            failure: ResponseFailed | None = None
            try:
                cancel.checkpoint()
                stream = await wait_for_io(
                    self._client.chat.completions.create(
                        **cast(CompletionCreateParamsStreaming, body)
                    ),
                    cancel,
                    deadline,
                )
                content_type = stream.response.headers.get("content-type", "").split(";")[0]
                if content_type.strip().lower() != "text/event-stream":
                    raise InvalidWireData("响应不是 SSE")
                state = ChatStream(
                    request,
                    names,
                    parallel=self.config.capabilities.parallel_tool_calls,
                    attempt_id=attempt_id,
                )
                while True:
                    try:
                        chunk = await wait_for_io(anext(stream), cancel, deadline)
                    except StopAsyncIteration:
                        break
                    for event in state.feed(chunk):
                        cancel.checkpoint()
                        exposed = exposed or not isinstance(event, ModelUsageObserved)
                        yield event
                bounded = stream.response.extensions.get("harnessix_stream")
                if not isinstance(bounded, BoundedStream):
                    raise InvalidWireData("响应缺少传输预算")
                completed = state.finish(seen_done=bounded.seen_done)
                yield finish_attempt(attempt_id)
                for event in completed:
                    cancel.checkpoint()
                    exposed = True
                    yield event
                return
            except TurnCancelled:
                raise
            except Exception as error:
                failure = _failure(error)
            finally:
                if stream is not None:
                    try:
                        async with asyncio.timeout(min(5, self.config.io_timeout_seconds)):
                            await stream.close()
                    except Exception:
                        # 清理故障不能泄露原始异常或覆盖取消；SDK/HTTPX 本身仍负责关闭连接池。
                        pass
            yield finish_attempt(attempt_id, failure or ResponseFailed(code="unknown"))
            if (
                failure is None
                or exposed
                or not failure.retryable
                or attempt + 1 == self.config.max_attempts
                or asyncio.get_running_loop().time() >= deadline
            ):
                yield failure or ResponseFailed(code="unknown")
                return
            try:
                await wait_for_io(
                    asyncio.sleep(self.config.retry_delay_seconds * (2**attempt)), cancel, deadline
                )
            except TimeoutError:
                yield ResponseFailed(code="transport", retryable=True)
                return
