"""模型Provider适配：在Anthropic事件与Provider中立合同间转换。"""

from __future__ import annotations

from typing import Any

from harnessix.agent.models import ToolCallContent, ToolCallRejectionContent, ToolResultContent
from harnessix.models._history import (
    InvalidModelRequest,
    budget_instruction,
    current_tool_catalog_instruction,
    encode_json,
    messages_for,
    wire_aliases,
    wire_directory,
)
from harnessix.models._json import strict_json
from harnessix.models._tool_schema import provider_tool_schema
from harnessix.models.config import AnthropicConfig
from harnessix.models.contracts import ModelRequest


def build_request(
    request: ModelRequest, config: AnthropicConfig
) -> tuple[dict[str, Any], dict[str, str]]:
    if not config.capabilities.tool_calls and (
        request.tools
        or any(
            isinstance(i.content, ToolCallContent | ToolCallRejectionContent | ToolResultContent)
            for i in request.history
        )
    ):
        raise InvalidModelRequest("当前 Provider 不支持工具调用")
    messages: list[dict[str, Any]] = []
    for message in messages_for(request):
        role = message["role"]
        blocks: list[dict[str, Any]] = []
        if role == "tool":
            role = "user"
            result = strict_json(message["content"])
            assert isinstance(result, dict)
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message["content"],
                    "is_error": result["outcome"] != "succeeded",
                }
            )
        else:
            if message["content"]:
                blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls", []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": strict_json(call["function"]["arguments"]),
                    }
                )
        if not blocks:
            raise InvalidModelRequest("Anthropic 不接受空消息")
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    if messages[0]["role"] != "user" or messages[-1]["role"] != "user":
        raise InvalidModelRequest("Anthropic History 必须以 user 开始和结束，不支持 prefill")
    names = wire_directory(request)
    tools: list[dict[str, Any]] = [
        {
            "name": alias,
            "description": definition.name + ": " + definition.description,
            "input_schema": provider_tool_schema(definition.input_schema),
        }
        for alias, definition in zip(names, request.wire_tools, strict=True)
    ]
    body: dict[str, Any] = {
        "model": config.model,
        "max_tokens": min(
            config.max_output_tokens, request.remaining_tokens or request.budget.max_tokens
        ),
        "messages": messages,
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    system = tuple(
        note
        for note in (
            request.instructions,
            budget_instruction(request),
            current_tool_catalog_instruction(
                request,
                [alias for alias, name in names.items() if name in {t.name for t in request.tools}],
            ),
        )
        if note
    )
    if system:
        body["system"] = "\n\n".join(system)
    if tools:
        body["tools"] = tools
        body["tool_choice"] = {
            "type": (
                "tool"
                if request.forced_tool is not None
                else "any"
                if request.tool_choice == "required"
                else "auto"
            ),
            "disable_parallel_tool_use": not config.capabilities.parallel_tool_calls,
        }
        if request.forced_tool is not None:
            body["tool_choice"]["name"] = wire_aliases(request)[request.forced_tool]
    if len(encode_json(body).encode()) > config.max_request_bytes:
        raise InvalidModelRequest("模型请求超过大小上限")
    return body, names
