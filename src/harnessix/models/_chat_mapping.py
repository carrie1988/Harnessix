from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from harnessix.agent.models import ItemStatus, TextContent, ToolCallContent, ToolResultContent
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest


class InvalidModelRequest(ValueError):
    pass


def tool_alias(name: str) -> str:
    return "hx_" + hashlib.sha256(name.encode()).hexdigest()[:60]


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def messages_for(request: ModelRequest) -> list[dict[str, Any]]:
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
                        content.model_dump(mode="json", include={"outcome", "output", "error"})
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


def build_request(
    request: ModelRequest, config: OpenAIChatConfig
) -> tuple[dict[str, Any], dict[str, str]]:
    names: dict[str, str] = {}
    tools: list[dict[str, Any]] = []
    if not config.capabilities.tool_calls and (
        request.tools
        or any(isinstance(i.content, ToolCallContent | ToolResultContent) for i in request.history)
    ):
        raise InvalidModelRequest("当前 Provider 不支持工具调用")
    for definition in request.tools:
        alias = tool_alias(definition.name)
        if (
            not definition.name
            or len(definition.name) > 256
            or alias in names
            or definition.input_schema.get("type") != "object"
        ):
            raise InvalidModelRequest("工具名称或输入 Schema 无效")
        names[alias] = definition.name
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": alias,
                    "description": definition.name + ": " + definition.description,
                    "parameters": definition.input_schema,
                },
            }
        )
    remaining = request.remaining_tokens or request.budget.max_tokens
    body: dict[str, Any] = {
        "model": config.model,
        "messages": messages_for(request),
        "stream": True,
        "stream_options": {"include_usage": True},
        config.output_token_parameter: min(config.max_output_tokens, remaining),
    }
    if tools:
        body["tools"] = tools
        body["parallel_tool_calls"] = config.capabilities.parallel_tool_calls
    if len(encode_json(body).encode()) > config.max_request_bytes:
        raise InvalidModelRequest("模型请求超过大小上限")
    return body, names
