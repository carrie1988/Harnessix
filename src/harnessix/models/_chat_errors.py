"""Chat终态失败：内部封闭原因与原尝试账本的低敏投影，不保存响应正文。"""

from __future__ import annotations

from enum import StrEnum

import httpx
from openai import APIConnectionError, APIError, APIStatusError

from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models._bounded_http import InvalidWireData


class ChatProtocolReason(StrEnum):
    COMPLETION_INCOMPLETE = "completion_incomplete"
    FINISH_REASON_UNSUPPORTED = "finish_reason_unsupported"
    FINISH_TOOL_MISMATCH = "finish_tool_mismatch"
    SEMANTIC_OUTPUT_MISSING = "semantic_output_missing"
    TOOL_INDEX_GAP = "tool_index_gap"
    TOOL_ID_MISSING = "tool_id_missing"
    TOOL_ID_DUPLICATE = "tool_id_duplicate"
    TOOL_NAME_UNKNOWN = "tool_name_unknown"
    TOOL_TYPE_INVALID = "tool_type_invalid"
    TOOL_ARGUMENTS_INVALID = "tool_arguments_invalid"
    TOOL_ARGUMENTS_NOT_OBJECT = "tool_arguments_not_object"


class ChatProtocolError(InvalidWireData):
    """固定异常正文及准确Enum类型；供应商文本不能充当诊断原因。"""

    def __init__(self, reason: ChatProtocolReason) -> None:
        if type(reason) is not ChatProtocolReason:
            raise ValueError("Chat协议诊断原因类型无效")
        super().__init__("Chat终态不符合协议")
        self._reason = reason

    @property
    def reason(self) -> ChatProtocolReason:
        return self._reason


class ChatTransportReason(StrEnum):
    HTTPX_CONNECT_TIMEOUT = "httpx_connect_timeout"
    HTTPX_READ_TIMEOUT = "httpx_read_timeout"
    HTTPX_WRITE_TIMEOUT = "httpx_write_timeout"
    HTTPX_POOL_TIMEOUT = "httpx_pool_timeout"
    HTTPX_CONNECT_FAILURE = "httpx_connect_failure"
    HTTPX_READ_FAILURE = "httpx_read_failure"
    HTTPX_WRITE_FAILURE = "httpx_write_failure"
    HTTPX_PROXY_FAILURE = "httpx_proxy_failure"
    HTTPX_PROTOCOL_FAILURE = "httpx_protocol_failure"
    TIMEOUT_ORIGIN_UNKNOWN = "timeout_origin_unknown"
    HTTP_STATUS_408 = "http_status_408"
    HTTP_STATUS_409 = "http_status_409"


_HTTPX_TRANSPORT_REASONS = {
    httpx.ConnectTimeout: ChatTransportReason.HTTPX_CONNECT_TIMEOUT,
    httpx.ReadTimeout: ChatTransportReason.HTTPX_READ_TIMEOUT,
    httpx.WriteTimeout: ChatTransportReason.HTTPX_WRITE_TIMEOUT,
    httpx.PoolTimeout: ChatTransportReason.HTTPX_POOL_TIMEOUT,
    httpx.ConnectError: ChatTransportReason.HTTPX_CONNECT_FAILURE,
    httpx.ReadError: ChatTransportReason.HTTPX_READ_FAILURE,
    httpx.WriteError: ChatTransportReason.HTTPX_WRITE_FAILURE,
    httpx.ProxyError: ChatTransportReason.HTTPX_PROXY_FAILURE,
    httpx.ProtocolError: ChatTransportReason.HTTPX_PROTOCOL_FAILURE,
    httpx.LocalProtocolError: ChatTransportReason.HTTPX_PROTOCOL_FAILURE,
    httpx.RemoteProtocolError: ChatTransportReason.HTTPX_PROTOCOL_FAILURE,
}


def _transport_reason(error: Exception) -> ChatTransportReason | None:
    if isinstance(error, APIStatusError):
        status = error.status_code
        if type(status) is not int or status != error.response.status_code:
            return None
        return {
            408: ChatTransportReason.HTTP_STATUS_408,
            409: ChatTransportReason.HTTP_STATUS_409,
        }.get(status)
    # 仅类型不能区分asyncio期限和底层TimeoutError；不据此推断来源。
    if type(error) is TimeoutError:
        return ChatTransportReason.TIMEOUT_ORIGIN_UNKNOWN
    cause = error.__cause__ if isinstance(error, APIConnectionError) else error
    # 只读取一层cause和已知原生类型；不追溯OS异常、上下文或供应商自由文本。
    if not isinstance(cause, httpx.TransportError):
        return None
    return _HTTPX_TRANSPORT_REASONS.get(type(cause))


def diagnostic_failure(error: Exception, attempt: ModelAttemptFinished) -> ModelAttemptFinished:
    """只细化原失败消息；保持原code、类别、重试和尝试身份。"""
    if attempt.error is None:
        return attempt
    if attempt.error.code == "provider_invalid_provider_output":
        cause = error.__cause__ if isinstance(error, APIError) else error
        if type(cause) is not ChatProtocolError or type(cause.reason) is not ChatProtocolReason:
            return attempt
        diagnostic = f"chat_protocol/v1:{cause.reason.value}"
    elif attempt.error.code == "provider_transport":
        reason = _transport_reason(error)
        if reason is None:
            return attempt
        diagnostic = f"chat_transport/v1:{reason.value}"
    else:
        return attempt
    failure = attempt.error.model_copy(update={"message": f"Provider 返回结构化失败；{diagnostic}"})
    return attempt.model_copy(update={"error": failure})
