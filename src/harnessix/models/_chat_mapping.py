"""模型Provider适配：在Chat Completions消息与Provider中立合同间转换。"""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

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
from harnessix.models._tool_schema import provider_tool_schema
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ModelRequest


def _thinking_switch(
    config: OpenAIChatConfig, thinking: bool, output_tokens: int
) -> dict[str, JsonValue]:
    """顶层enable_thinking格式的思考参数：开关，以及随开启思考的请求发送的上限或强度档位。"""
    switch: dict[str, JsonValue] = {"enable_thinking": thinking}
    if thinking and config.thinking_budget is not None:
        if output_tokens < 2:
            raise InvalidModelRequest("整体输出预算不足以同时容纳思考与回答")
        # 配置是上限；输出收缩时仍为可见回答留出余量，不扩张任务预算。
        switch["thinking_budget"] = min(config.thinking_budget, output_tokens - 1)
    if thinking and config.reasoning_effort is not None:
        switch["reasoning_effort"] = config.reasoning_effort
    return switch


def build_request(
    request: ModelRequest, config: OpenAIChatConfig, *, include_catalog_note: bool = True
) -> tuple[dict[str, Any], dict[str, str]]:
    if not config.capabilities.tool_calls and (
        request.tools
        or any(
            isinstance(i.content, ToolCallContent | ToolCallRejectionContent | ToolResultContent)
            for i in request.history
        )
    ):
        raise InvalidModelRequest("当前 Provider 不支持工具调用")
    names = wire_directory(request)
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": alias,
                "description": definition.name + ": " + definition.description,
                "parameters": provider_tool_schema(definition.input_schema),
            },
        }
        for alias, definition in zip(names, request.wire_tools, strict=True)
    ]
    remaining = request.remaining_tokens or request.budget.max_tokens
    messages = messages_for(request, tool_result_format=config.tool_result_format)
    notes: list[str] = []
    if include_catalog_note:
        catalog_note = current_tool_catalog_instruction(request, _callable_aliases(request, names))
        if catalog_note is not None:
            notes.append(catalog_note)
    budget_note = budget_instruction(request)
    if budget_note is not None:
        notes.append(budget_note)
    if config.prompt_cache == "explicit_tail":
        messages = _explicit_tail_cache(request.instructions, messages, notes)
    elif config.prompt_cache == "stable_tail":
        if request.instructions is not None:
            messages.insert(0, {"role": "system", "content": request.instructions})
        # 自动缓存端点会把所有system消息合并到开头；逐请求说明须以末尾user消息呈现，前缀才保持不变。
        messages.extend({"role": "user", "content": _HARNESS_NOTE + note} for note in notes)
    else:
        for note in notes:
            messages.insert(0, {"role": "system", "content": note})
        if request.instructions is not None:
            messages.insert(0, {"role": "system", "content": request.instructions})
    # 按工具步骤思考时，无工具请求（摘要、收尾回复）不思考。
    thinking = bool(config.enable_thinking) and not (config.thinking_tool_steps_only and not tools)
    native_thinking = config.thinking_format == "thinking_type" and thinking
    if native_thinking:
        # 思考模式下带工具的历史助手消息须携带reasoning_content；不回传推理内容时以空串满足协议。
        messages = [
            {**message, "reasoning_content": message.get("reasoning_content", "")}
            if message.get("role") == "assistant"
            else message
            for message in messages
        ]
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
        # 思考模式只支持auto、或端点强制选择不可靠：强制与required改由可执行集收窄与目录说明约束，
        # 违约调用沿拒绝路径。
        tool_choice = (
            None
            if native_thinking or config.omit_tool_choice
            else _wire_tool_choice(request, function_choice=config.prompt_cache != "stable_tail")
        )
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    if config.thinking_format == "thinking_type":
        body["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    elif config.thinking_format == "chat_template_kwargs":
        # vLLM把chat_template_kwargs交给模型对话模板（如Qwen3系的enable_thinking）。
        body["extra_body"] = {"chat_template_kwargs": {"enable_thinking": thinking}}
    elif config.enable_thinking is not None:
        # OpenAI SDK 将 extra_body 合并到最终请求顶层，供兼容端点接收扩展参数。
        body["extra_body"] = _thinking_switch(config, thinking, output_tokens)
    if len(encode_json(body).encode()) > config.max_request_bytes:
        raise InvalidModelRequest("模型请求超过大小上限")
    return body, names


_CACHE_MARKER: dict[str, str] = {"type": "ephemeral"}


_HARNESS_NOTE = "[Harness note] "


def _wire_tool_choice(
    request: ModelRequest, *, function_choice: bool = True
) -> str | dict[str, Any] | None:
    """自动缓存端点的指定函数选择会改写提示渲染、使缓存失效，改为required并由可执行集收窄约束。"""
    if request.forced_tool is not None and function_choice:
        forced = wire_aliases(request)[request.forced_tool]
        return {"type": "function", "function": {"name": forced}}
    return "required" if request.tool_choice == "required" else None


def _explicit_tail_cache(
    instructions: str | None, history: list[dict[str, Any]], notes: list[str]
) -> list[dict[str, Any]]:
    """指令与历史构成跨请求不变的前缀并在末端打缓存标记；易变说明放在标记之后。

    前缀内所有文本消息恒用同一块形态，标记移动后上一请求的消息仍逐字节一致（Provider
    前缀匹配不比较cache_control）；只改变形态与顺序，不改变文本；最多两个标记。
    """
    messages: list[dict[str, Any]] = []
    if instructions is not None:
        messages.append({"role": "system", "content": _blocks(instructions, marked=True)})
    for message in history:
        content = message.get("content")
        messages.append(
            {**message, "content": _blocks(content)}
            if isinstance(content, str) and content
            else message
        )
    for index in range(len(messages) - 1, 0, -1):
        content = messages[index].get("content")
        if isinstance(content, list):
            messages[index] = {
                **messages[index],
                "content": _blocks(content[0]["text"], marked=True),
            }
            break
    messages.extend({"role": "system", "content": note} for note in notes)
    return messages


def _blocks(text: str, *, marked: bool = False) -> list[dict[str, Any]]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if marked:
        block["cache_control"] = dict(_CACHE_MARKER)
    return [block]


def _callable_aliases(request: ModelRequest, names: dict[str, str]) -> list[str]:
    """稳定目录下说明只列出本步可执行的别名；未设目录时与线上目录相同。"""
    callable_names = {definition.name for definition in request.tools}
    return [alias for alias, name in names.items() if name in callable_names]
