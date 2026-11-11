"""模型Provider适配：将持久Agent历史转换为Provider输入消息。"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Literal
from uuid import UUID

from harnessix.agent.completion_guard import without_non_answers
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
)
from harnessix.agent.tool_rejections import (
    RejectionReason,
    rejection_feedback,
    require_rejection_result,
)
from harnessix.models.contracts import ModelRequest

# encode_json在tool_result_text中定义，此处显式再导出供既有调用方使用。
from harnessix.models.tool_result_text import encode_json as encode_json
from harnessix.models.tool_result_text import tool_result_text


class InvalidModelRequest(ValueError):
    pass


REJECTED_TOOL_SENTINEL = "harnessix_rejected_tool_v1"
_WIRE_NAME_LIMIT = 64
_SUFFIX_HEX_DIGITS = 8


def _readable(name: str, limit: int) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:limit] or "tool"


def _suffixed(name: str) -> str:
    """可读片段加原名精确UTF-8字节摘要的前8位；摘要不做Unicode规范化。"""
    stem = _readable(name, _WIRE_NAME_LIMIT - _SUFFIX_HEX_DIGITS - 1)
    return f"{stem}_{hashlib.sha256(name.encode()).hexdigest()[:_SUFFIX_HEX_DIGITS]}"


def tool_alias(name: str) -> str:
    """单个工具名的可读线协议别名，不作为持久身份或执行权限。

    合法原名不变，其余字符逐个替换为下划线并截到64字符；模型照着工具名即可写对，
    不必抄写摘要。不同原名可能得到同一别名，目录内的一一对应由tool_aliases保证。
    拒绝哨兵只表达历史，别名与它相同的工具恒加后缀。
    """
    alias = _readable(name, _WIRE_NAME_LIMIT)
    return _suffixed(name) if alias == REJECTED_TOOL_SENTINEL else alias


def tool_aliases(names: Iterable[str]) -> dict[str, str]:
    """一份目录内原名到别名的一一映射，只由目录内的名字集合决定，与顺序无关。

    可读别名互不相同时直接使用；多个原名替换后相同时，本身就是该别名的合法原名保留它，
    其余加摘要后缀。加后缀后仍相同的目录由调用方整体拒绝。
    """
    readable = {name: tool_alias(name) for name in names}
    shared = {alias for alias, count in Counter(readable.values()).items() if count > 1}
    return {
        name: alias if alias not in shared or alias == name else _suffixed(name)
        for name, alias in readable.items()
    }


def wire_aliases(request: ModelRequest) -> dict[str, str]:
    """本次请求线上目录的原名到别名映射；工具定义、强制选择与历史投影共用。"""
    return tool_aliases(definition.name for definition in request.wire_tools)


def wire_directory(request: ModelRequest) -> dict[str, str]:
    """本次线上目录的别名到原名反向映射，顺序同目录；名称、Schema或别名冲突时整体拒绝。"""
    aliases = wire_aliases(request)
    names: dict[str, str] = {}
    for definition in request.wire_tools:
        alias = aliases[definition.name]
        if (
            not definition.name
            or len(definition.name) > 256
            or alias in names
            or definition.input_schema.get("type") != "object"
        ):
            raise InvalidModelRequest("工具名称或输入 Schema 无效")
        names[alias] = definition.name
    return names


def _history_alias(tool: str, aliases: Mapping[str, str]) -> str:
    """历史调用沿用本次目录的别名；目录外的旧工具若与目录别名相同则加后缀，不让旧调用看似指向当前工具。"""
    alias = aliases.get(tool)
    if alias is None:
        alias = tool_alias(tool)
        if alias in aliases.values():
            alias = _suffixed(tool)
    return alias


def current_tool_catalog_instruction(request: ModelRequest, aliases: Iterable[str]) -> str | None:
    """仅用本次广告别名澄清历史边界，不复制历史名称、参数或执行权限。"""
    if not any(
        isinstance(item.content, ToolCallContent | ToolCallRejectionContent)
        for item in request.history
    ):
        return None
    current = tuple(aliases)
    note = (
        f"Current request tool catalog (wire aliases): {encode_json(current)}\n"
        "Only these aliases are callable in this request; historical tool calls do not grant "
        "current permission. harnessix_rejected_tool_v1 is a history-only rejection sentinel, "
        "not a callable tool. "
    )
    if not current:
        note += "No tools are callable. "
    if request.tool_notice is not None:
        note += f"Why the catalog is restricted now: {request.tool_notice} "
    if request.forced_tool is not None:
        forced = wire_aliases(request)[request.forced_tool]
        return note + (
            f"Tool choice: forced ({encode_json(forced)}). You must "
            "call this tool; a text-only completion does not satisfy forced tool choice."
        )
    if request.tool_choice == "required":
        return note + (
            "Tool choice: required. You must call at least one tool from this catalog; "
            "a text-only completion does not satisfy required tool choice."
        )
    return note + (
        "Tool choice: auto. Tool use is optional; if available tools cannot provide enough "
        "evidence, end honestly with the limitation rather than inventing tools or results."
    )


def budget_instruction(request: ModelRequest) -> str | None:
    """宿主账本的当前事实，不替代 Runtime 的预算准入或实际用量核验。"""
    if request.consumed_usage is None:
        return None
    remaining = max(0, request.budget.max_tokens - request.consumed_usage.total_tokens)
    steps = (
        request.remaining_calls
        if request.remaining_calls is not None
        else max(0, request.budget.max_steps - request.step + 1)
    )
    note = (
        f"Runtime budget: {remaining} cumulative input+output tokens and {steps} model calls "
        "remain, including this request and repeated history. Use the current catalog only. "
        "Budget investigation, patch, validation, possible repair and review together; batch "
        "independent reads and avoid repeated no-match searches."
    )
    if request.input_tokens_upper_bound is not None:
        note += (
            f" This request's conservative input bound is {request.input_tokens_upper_bound} "
            "tokens, not observed usage. Repeated input can exhaust the turn before the call "
            "limit; reserve input and output for validation, repair and final review. "
            "Narrow investigation when needed; never patch without source evidence."
        )
    if request.affordable_requests is not None:
        note += (
            f" At the size of the last request about {request.affordable_requests} requests fit, "
            "including this one; patch, run_profile and review need at least three, so patch as "
            "soon as observed source shows the cause."
        )
    return note


def _public_result(content: ToolResultContent, rejection: RejectionReason | None) -> dict[str, Any]:
    """工具结果发给模型的公开字段；目录拒绝的固定结果只把说明换成给模型的英文反馈。

    持久结果、回放校验与界面不变；反馈由拒绝原因唯一确定，不随当前目录变化，历史前缀保持稳定。
    """
    fields = {"outcome", "output", "error", "diff_artifact"}
    if rejection is None:
        return content.model_dump(mode="json", include=fields)
    # 先核对固定合同再序列化：不符合合同的拒绝结果不进入任何线上正文。
    try:
        require_rejection_result(content)
    except KernelError:
        raise InvalidModelRequest("拒绝工具结果不符合固定合同") from None
    public = content.model_dump(mode="json", include=fields)
    public["error"]["message"] = rejection_feedback(rejection)
    return public


def messages_for(
    request: ModelRequest, *, tool_result_format: Literal["json", "text"] = "json"
) -> list[dict[str, Any]]:
    """私有规范化视图：完成消息、工具组及配对；不包含任何 SDK 对象。

    工具结果默认为规范JSON；text只改变发给模型的正文，持久结果与回放不变。
    """
    messages: list[dict[str, Any]] = []
    assistant: dict[str, Any] | None = None
    pending: set[UUID] = set()
    rejected: dict[UUID, RejectionReason] = {}
    seen: set[UUID] = set()
    taking_results = False
    aliases = wire_aliases(request)
    for item in without_non_answers(request.history):
        if item.status != ItemStatus.COMPLETED:
            raise InvalidModelRequest("History 含未完成 Item")
        content = item.content
        if isinstance(content, ToolResultContent):
            if content.call_id not in pending:
                raise InvalidModelRequest("工具结果缺少唯一配对调用")
            public = _public_result(content, rejected.get(content.call_id))
            if assistant is not None:
                messages.append(assistant)
                assistant = None
            taking_results = True
            pending.remove(content.call_id)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": "call_" + content.call_id.hex,
                    "content": (tool_result_text if tool_result_format == "text" else encode_json)(
                        public
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
        elif isinstance(content, ToolCallContent | ToolCallRejectionContent):
            if content.call_id in seen:
                raise InvalidModelRequest("工具调用身份重复")
            pending.add(content.call_id)
            seen.add(content.call_id)
            if isinstance(content, ToolCallRejectionContent):
                rejected[content.call_id] = content.reason
            if assistant is None:
                assistant = {"role": "assistant", "content": ""}
            assistant.setdefault("tool_calls", []).append(
                {
                    "id": "call_" + content.call_id.hex,
                    "type": "function",
                    "function": {
                        # 拒绝标记仅表达闭合历史，不参与广告或重新计算别名。
                        "name": _history_alias(content.tool, aliases)
                        if isinstance(content, ToolCallContent)
                        else REJECTED_TOOL_SENTINEL,
                        "arguments": encode_json(content.arguments)
                        if isinstance(content, ToolCallContent)
                        else "{}",
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
