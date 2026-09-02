from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Self, cast

import httpx2
from anthropic import APIConnectionError, APIError, APIStatusError, AsyncAnthropic, AsyncStream
from anthropic.types import RawMessageStreamEvent
from anthropic.types.message_create_params import MessageCreateParamsStreaming

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.models._anthropic_http import AnthropicTransport
from harnessix.models._anthropic_mapping import build_request
from harnessix.models._anthropic_stream import AnthropicStream
from harnessix.models._bounded_http import InvalidWireData
from harnessix.models._provider_io import read_key, wait_for_io
from harnessix.models.config import AnthropicConfig
from harnessix.models.contracts import ModelRequest, ProviderEvent, ResponseFailed


def _failure(error: Exception) -> ResponseFailed:
    if isinstance(error, APIError) and isinstance(error.__cause__, InvalidWireData):
        return ResponseFailed(code="invalid_provider_output")
    if isinstance(error, APIConnectionError | httpx2.TransportError | TimeoutError):
        return ResponseFailed(code="transport", retryable=True)
    if isinstance(error, APIStatusError):
        body = error.body
        detail = body.get("error") if isinstance(body, dict) else None
        kind = detail.get("type") if isinstance(detail, dict) else None
        if kind in ("authentication_error", "permission_error") or error.status_code in (401, 403):
            return ResponseFailed(code="authentication")
        if kind == "billing_error" or error.status_code == 402:
            return ResponseFailed(code="quota")
        if kind == "rate_limit_error" or error.status_code == 429:
            return ResponseFailed(code="rate_limit", retryable=True)
        if kind == "timeout_error" or error.status_code in (408, 504):
            return ResponseFailed(code="transport", retryable=True)
        if kind in ("overloaded_error", "api_error") or error.status_code >= 500:
            return ResponseFailed(code="provider_internal", retryable=True)
        if 200 <= error.status_code < 300 and kind not in (
            "invalid_request_error",
            "not_found_error",
            "request_too_large",
            "conflict_error",
        ):
            return ResponseFailed(code="unknown")
        return ResponseFailed(code="invalid_request")
    return ResponseFailed(code="invalid_provider_output")


class AnthropicProvider:
    """仅支持非 Thinking 的 Messages 配置；拥有独立 SDK 和 HTTPX2 Client。"""

    def __init__(
        self, config: AnthropicConfig, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.config = AnthropicConfig.model_validate_json(config.model_dump_json())
        key = read_key(self.config, headers_env="ANTHROPIC_CUSTOM_HEADERS")
        client = httpx2.AsyncClient(
            transport=AnthropicTransport(
                transport or httpx2.AsyncHTTPTransport(trust_env=False), self.config
            ),
            trust_env=False,
            follow_redirects=False,
            timeout=config.io_timeout_seconds,
        )
        self._client = AsyncAnthropic(
            api_key=key,
            webhook_key="",
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
            stream: AsyncStream[RawMessageStreamEvent] | None = None
            failure: ResponseFailed | None = None
            try:
                cancel.checkpoint()
                stream = await wait_for_io(
                    self._client.messages.create(**cast(MessageCreateParamsStreaming, body)),
                    cancel,
                    deadline,
                )
                content_type = stream.response.headers.get("content-type", "").split(";")[0]
                if content_type.strip().lower() != "text/event-stream":
                    raise InvalidWireData("响应不是 SSE")
                state = AnthropicStream(
                    request, names, parallel=self.config.capabilities.parallel_tool_calls
                )
                while True:
                    try:
                        chunk = await wait_for_io(anext(stream), cancel, deadline)
                    except StopAsyncIteration:
                        break
                    for event in state.feed(chunk):
                        cancel.checkpoint()
                        exposed = True
                        yield event
                for event in state.finish():
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
                        pass
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
