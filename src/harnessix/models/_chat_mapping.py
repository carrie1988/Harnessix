"""模型Provider适配：在Chat Completions消息与Provider中立合同间转换。"""

from __future__ import annotations

from typing import Any

from harnessix.agent.models import ToolCallContent, ToolCallRejectionContent, ToolResultContent
from harnessix.models._history import (
    InvalidModelRequest,
    budget_instruction,
    encode_json,
    messages_for,
    tool_alias,
)
from harnessix.models._tool_schema import provider_tool_schema
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest


def build_request(
    request: ModelRequest, config: OpenAIChatConfig
) -> tuple[dict[str, Any], dict[str, str]]:
    names: dict[str, str] = {}
    tools: list[dict[str, Any]] = []
    if not config.capabilities.tool_calls and (
        request.tools
        or any(
            isinstance(i.content, ToolCallContent | ToolCallRejectionContent | ToolResultContent)
            for i in request.history
        )
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
                    "parameters": provider_tool_schema(definition.input_schema),
                },
            }
        )
    remaining = request.remaining_tokens or request.budget.max_tokens
    messages = messages_for(request)
    budget_note = budget_instruction(request)
    if budget_note is not None:
        messages.insert(0, {"role": "system", "content": budget_note})
    if request.instructions is not None:
        messages.insert(0, {"role": "system", "content": request.instructions})
    output_tokens = min(config.max_output_tokens, remaining)
    body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        config.output_token_parameter: output_tokens,
    }
    if tools:
        body["tools"] = tools
        body["parallel_tool_calls"] = config.capabilities.parallel_tool_calls
        if request.forced_tool is not None:
            body["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_alias(request.forced_tool)},
            }
        elif request.tool_choice == "required":
            body["tool_choice"] = "required"
    if config.enable_thinking is not None:
        # OpenAI SDK 将 extra_body 合并到最终请求顶层，供兼容端点接收扩展参数。
        body["extra_body"] = {"enable_thinking": config.enable_thinking}
        if config.thinking_budget is not None:
            if output_tokens < 2:
                raise InvalidModelRequest("整体输出预算不足以同时容纳思考与回答")
            # 配置是上限；输出收缩时仍为可见回答留出余量，不扩张任务预算。
            body["extra_body"]["thinking_budget"] = min(config.thinking_budget, output_tokens - 1)
    if len(encode_json(body).encode()) > config.max_request_bytes:
        raise InvalidModelRequest("模型请求超过大小上限")
    return body, names
