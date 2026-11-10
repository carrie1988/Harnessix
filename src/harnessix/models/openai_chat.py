"""模型Provider适配：实现OpenAI-compatible Chat Completions Provider端口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Any, Literal, Self, cast
from uuid import UUID

import httpx
from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.completion_create_params import CompletionCreateParamsStreaming

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.ids import new_id
from harnessix.agent.model_input_budget import INCREMENTAL_REQUEST_OVERHEAD_TOKENS
from harnessix.agent.models import Item
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models._bounded_http import BoundedStream, BoundedTransport, InvalidWireData
from harnessix.models._chat_errors import diagnostic_failure
from harnessix.models._chat_input_anchor import CompletedChatInput
from harnessix.models._chat_mapping import build_request
from harnessix.models._chat_stream import ChatStream, ContentRefused, validate_frame
from harnessix.models._history import InvalidModelRequest, encode_json
from harnessix.models._provider_io import (
    finish_attempt,
    read_key,
    retry_after_seconds,
    retry_delay_seconds,
    unbilled_usage,
    validate_key,
    wait_for_io,
)
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
)


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


def _is_unbilled(error: Exception) -> bool:
    """收到HTTP错误响应，或TCP/TLS连接未建立：请求没有被模型处理。"""

    if isinstance(error, APIStatusError):
        return error.status_code >= 400
    cause = error.__cause__ if isinstance(error, APIConnectionError) else error
    return isinstance(cause, httpx.ConnectError | httpx.ConnectTimeout)


def _failed_response(
    attempt_id: UUID, error: Exception
) -> tuple[ResponseFailed, ModelAttemptFinished]:
    """复用原错误分类及终态构造，只附加内部受控协议诊断。"""
    failure = _failure(error)
    return failure, diagnostic_failure(error, finish_attempt(attempt_id, failure))


class OpenAIChatProvider:
    """拥有HTTP Client的Chat Completions Adapter；支持宿主注入短生命周期认证。"""

    compaction_summary_projection: Literal["native-document-json-string/v1"] = (
        "native-document-json-string/v1"
    )

    def __init__(
        self,
        config: OpenAIChatConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        api_key: str | None = None,
    ) -> None:
        self.config = OpenAIChatConfig.model_validate_json(config.model_dump_json())
        key = (
            read_key(self.config, headers_env="OPENAI_CUSTOM_HEADERS")
            if api_key is None
            else validate_key(api_key, headers_env="OPENAI_CUSTOM_HEADERS")
        )
        client = httpx.AsyncClient(
            transport=BoundedTransport(
                transport
                or httpx.AsyncHTTPTransport(
                    trust_env=False,
                    retries=self.config.connect_retries,
                    proxy=self.config.proxy_url,
                ),
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
        self._completed_input: CompletedChatInput | None = None

    @property
    def input_token_identity(self) -> tuple[str, str]:
        return "openai_chat", self.config.model

    def estimate_input_tokens(self, request: ModelRequest, *, prefix_items: int = 0) -> int:
        """复用出站映射；extra_body 的额外 wrapper 也计入上界，不访问认证或 HTTP。"""
        if type(prefix_items) is not int or not 0 <= prefix_items <= len(request.history):
            raise InvalidModelRequest("输入预算前缀长度无效")
        body, _ = build_request(request, self.config)
        byte_count = len(encode_json(body).encode("utf-8"))
        if prefix_items:
            # 重启、改写、跨 Turn 或失败请求没有封装证明，仍全额计入当前工具和指令。
            prefix_body = self._history_body(request.history[:prefix_items], request)
            byte_count = max(0, byte_count - len(encode_json(prefix_body).encode("utf-8")))
            anchor = getattr(self, "_completed_input", None)
            if anchor is not None:
                redundant = anchor.unchanged_wrapper_bytes(
                    request, body, prefix_body, self.config, prefix_items
                )
                byte_count = max(0, byte_count - redundant)
        return byte_count + INCREMENTAL_REQUEST_OVERHEAD_TOKENS

    def estimate_rewritten_input_tokens(
        self, request: ModelRequest, previous_history: tuple[Item, ...]
    ) -> int:
        """同位置未变的历史消息已含在上一请求实际用量中；其余仍按出站字节全额计入。"""
        if not previous_history:
            raise InvalidModelRequest("改写前视图不能为空")
        body, _ = build_request(request, self.config)
        previous_body = self._history_body(previous_history, request)
        reused_bytes = sum(
            len(encode_json(current).encode("utf-8"))
            for current, previous in zip(
                self._history_body(request.history, request)["messages"],
                previous_body["messages"],
                strict=False,
            )
            if current == previous
        )
        byte_count = len(encode_json(body).encode("utf-8")) - reused_bytes
        anchor = getattr(self, "_completed_input", None)
        if anchor is not None:
            byte_count -= anchor.unchanged_wrapper_bytes(
                request, body, previous_body, self.config, len(previous_history)
            )
        return max(0, byte_count) + INCREMENTAL_REQUEST_OVERHEAD_TOKENS

    def _history_body(self, history: tuple[Item, ...], request: ModelRequest) -> dict[str, Any]:
        """只含历史消息的出站映射；工具、指令与动态说明不属于可复用历史。"""
        only_history = request.model_copy(
            update={
                "history": history,
                "tools": (),
                "instructions": None,
                "consumed_usage": None,
                "tool_choice": "auto",
                "forced_tool": None,
            }
        )
        body, _ = build_request(only_history, self.config, include_catalog_note=False)
        return body

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
        self._completed_input = None
        await self._client.close()

    async def probe_model_availability(self, cancel: CancelToken) -> bool:
        """只读查询当前凭据可见模型；调用方应在费用预留和推理请求前执行。"""
        cancel.checkpoint()
        if self._closed:
            raise RuntimeError("Provider 已关闭")
        deadline = asyncio.get_running_loop().time() + self.config.io_timeout_seconds
        page = await wait_for_io(self._client.models.list(), cancel, deadline)
        cancel.checkpoint()
        if page.has_next_page():
            raise InvalidWireData("模型目录分页无法完整判定")
        return any(model.id == self.config.model for model in page.data)

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        cancel.checkpoint()
        if self._closed:
            yield ResponseFailed(code="invalid_request")
            return
        self._completed_input = None
        try:
            body, names = build_request(request, self.config)
            input_candidate = CompletedChatInput.capture(request, body, self.config)
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
            unbilled = False
            retry_after = 0.0
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
                    repair_arguments=self.config.repair_tool_arguments,
                    implicit_cache=self.config.implicit_cache_usage,
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
                if any(
                    isinstance(event, ResponseCompleted)
                    and event.finish_reason in {"completed", "tool_calls"}
                    for event in completed
                ):
                    self._completed_input = input_candidate
                yield finish_attempt(attempt_id)
                for event in completed:
                    cancel.checkpoint()
                    exposed = True
                    yield event
                return
            except TurnCancelled:
                raise
            except Exception as error:
                failure, terminal = _failed_response(attempt_id, error)
                unbilled = stream is None and _is_unbilled(error)
                retry_after = retry_after_seconds(error)
            finally:
                if stream is not None:
                    try:
                        async with asyncio.timeout(min(5, self.config.io_timeout_seconds)):
                            await stream.close()
                    except Exception:
                        # 清理故障不能泄露原始异常或覆盖取消；SDK/HTTPX 本身仍负责关闭连接池。
                        pass
            if unbilled:
                yield unbilled_usage(attempt_id)
            yield terminal
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
                    asyncio.sleep(
                        retry_delay_seconds(self.config.retry_delay_seconds, attempt, retry_after)
                    ),
                    cancel,
                    deadline,
                )
            except TimeoutError:
                yield ResponseFailed(code="transport", retryable=True)
                return
