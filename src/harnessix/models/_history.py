"""模型Provider适配：将持久Agent历史转换为Provider输入消息。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    TextContent,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
)
from harnessix.agent.tool_rejections import require_rejection_result
from harnessix.models.contracts import ModelRequest


class InvalidModelRequest(ValueError):
    pass


def tool_alias(name: str) -> str:
    """生成可读线协议别名，不作为持久身份或执行权限。

    摘要基于精确原名的UTF-8字节，不做规范化。
    """
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "tool"
    return f"hx_{stem[:27]}_{hashlib.sha256(name.encode()).hexdigest()[:32]}"


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def messages_for(request: ModelRequest) -> list[dict[str, Any]]:
    """私有规范化视图：完成消息、工具组及配对；不包含任何 SDK 对象。"""
    messages: list[dict[str, Any]] = []
    assistant: dict[str, Any] | None = None
    pending: set[UUID] = set()
    rejected: set[UUID] = set()
    seen: set[UUID] = set()
    taking_results = False
    for item in request.history:
        if item.status != ItemStatus.COMPLETED:
            raise InvalidModelRequest("History 含未完成 Item")
        content = item.content
        if isinstance(content, ToolResultContent):
            if content.call_id not in pending:
                raise InvalidModelRequest("工具结果缺少唯一配对调用")
            if content.call_id in rejected:
                try:
                    require_rejection_result(content)
                except KernelError:
                    raise InvalidModelRequest("拒绝工具结果不符合固定合同") from None
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
        elif isinstance(content, ToolCallContent | ToolCallRejectionContent):
            if content.call_id in seen:
                raise InvalidModelRequest("工具调用身份重复")
            pending.add(content.call_id)
            seen.add(content.call_id)
            if isinstance(content, ToolCallRejectionContent):
                rejected.add(content.call_id)
            if assistant is None:
                assistant = {"role": "assistant", "content": ""}
            assistant.setdefault("tool_calls", []).append(
                {
                    "id": "call_" + content.call_id.hex,
                    "type": "function",
                    "function": {
                        # 拒绝标记仅表达闭合历史，不参与广告或重新计算别名。
                        "name": tool_alias(content.tool)
                        if isinstance(content, ToolCallContent)
                        else "harnessix_rejected_tool_v1",
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
