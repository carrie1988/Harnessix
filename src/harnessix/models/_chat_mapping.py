from __future__ import annotations

from typing import Any

from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.models._history import InvalidModelRequest, encode_json, messages_for, tool_alias
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest


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
