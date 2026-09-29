"""Chat终态失败：内部封闭原因与原尝试账本的低敏投影，不保存响应正文。"""

from __future__ import annotations

from enum import StrEnum

from openai import APIError

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


def diagnostic_failure(error: Exception, attempt: ModelAttemptFinished) -> ModelAttemptFinished:
    """只细化已判定协议失败的消息；保持原code、类别、重试和尝试身份。"""
    if attempt.error is None or attempt.error.code != "provider_invalid_provider_output":
        return attempt
    cause = error.__cause__ if isinstance(error, APIError) else error
    if type(cause) is not ChatProtocolError or type(cause.reason) is not ChatProtocolReason:
        return attempt
    failure = attempt.error.model_copy(
        update={"message": f"Provider 返回结构化失败；chat_protocol/v1:{cause.reason.value}"}
    )
    return attempt.model_copy(update={"error": failure})
