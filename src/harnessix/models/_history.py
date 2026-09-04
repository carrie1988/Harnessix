from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from harnessix.agent.models import ItemStatus, TextContent, ToolCallContent, ToolResultContent
from harnessix.models.contracts import ModelRequest


class InvalidModelRequest(ValueError):
    pass


def tool_alias(name: str) -> str:
    return "hx_" + hashlib.sha256(name.encode()).hexdigest()[:60]


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def messages_for(request: ModelRequest) -> list[dict[str, Any]]:
    """私有规范化视图：完成消息、工具组及配对；不包含任何 SDK 对象。"""
    messages: list[dict[str, Any]] = []
    assistant: dict[str, Any] | None = None
    pending: set[UUID] = set()
    seen: set[UUID] = set()
    taking_results = False
    for item in request.history:
        if item.status != ItemStatus.COMPLETED:
            raise InvalidModelRequest("History 含未完成 Item")
        content = item.content
        if isinstance(content, ToolResultContent):
            if content.call_id not in pending:
                raise InvalidModelRequest("工具结果缺少唯一配对调用")
            if assistant is not None:
                messages.append(assistant)
                assistant = None
            taking_results = True
            pending.remove(content.call_id)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": "call_" + content.call_id.hex,
                    "content": encode_json(
                        content.model_dump(
                            mode="json", include={"outcome", "output", "error", "diff_artifact"}
                        )
                    ),
                }
            )
            continue
        if taking_results and pending:
            raise InvalidModelRequest("工具结果组不可被其他消息打断")
        taking_results = False
        if isinstance(content, TextContent) and content.kind == "user_message":
            if pending:
                raise InvalidModelRequest("工具调用缺少结果")
            if assistant is not None:
                messages.append(assistant)
                assistant = None
            messages.append({"role": "user", "content": content.text})
        elif isinstance(content, TextContent) and content.kind == "assistant_message":
            if assistant is None:
                assistant = {"role": "assistant", "content": ""}
            assistant["content"] += content.text
        elif isinstance(content, ToolCallContent):
            if content.call_id in seen:
                raise InvalidModelRequest("工具调用身份重复")
            pending.add(content.call_id)
            seen.add(content.call_id)
            if assistant is None:
                assistant = {"role": "assistant", "content": ""}
            assistant.setdefault("tool_calls", []).append(
                {
                    "id": "call_" + content.call_id.hex,
                    "type": "function",
                    "function": {
                        "name": tool_alias(content.tool),
                        "arguments": encode_json(content.arguments),
                    },
                }
            )
        else:
            raise InvalidModelRequest("History 含不支持的 Item 类型")
    if pending:
        raise InvalidModelRequest("工具调用缺少结果")
    if assistant is not None:
        messages.append(assistant)
    if not messages:
        raise InvalidModelRequest("History 不能为空")
    return messages
