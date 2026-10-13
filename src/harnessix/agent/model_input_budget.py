"""重复模型请求的保守 Turn Token 预算估算。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Item, Turn
from harnessix.agent.usage import ModelAttempt
from harnessix.context.contracts import ContextInspectionRecord
from harnessix.context.engine import estimate_tokens
from harnessix.context.tool_result_contracts import ModelHistoryInspectionRecord
from harnessix.context.tool_result_view import history_document
from harnessix.domain.models import ToolDescriptor
from harnessix.models.contracts import ModelInputTokenEstimator, ModelProvider, ModelRequest

# 上一轮 Provider 用量已包含旧请求封装；为新增消息边界保留固定余量，
# 避免预算安全依赖某个 Provider 的私有 Tokenizer。
INCREMENTAL_REQUEST_OVERHEAD_TOKENS = 512


def estimate_model_input_tokens(
    history: Sequence[Item],
    tools: Sequence[ToolDescriptor],
    instructions: str | None,
    *,
    turn: Turn | None = None,
    model_step: int | None = None,
) -> int:
    """安全时以持久 Provider 用量为锚点估算下一请求。

    首次请求及历史改写使用 UTF-8 上界。对于仅追加历史的后续请求，以上一轮
    Provider 上报的实际输入为更紧的持久锚点，只累加新历史；旧事件没有工具
    目录指纹，因此当前工具目录始终全额计入。未变化的运行指令由持久 Context
    指纹证明，不保存其正文副本。
    """

    history_documents = tuple(history_document(item) for item in history)
    tool_documents = tuple(_tool_document(tool) for tool in tools)
    full_estimate = sum(estimate_tokens(document) for document in history_documents)
    full_estimate += sum(estimate_tokens(document) for document in tool_documents)
    full_estimate += estimate_tokens(instructions or "")
    if turn is None or model_step is None:
        return full_estimate

    anchor = _previous_completed_attempt(turn, model_step)
    previous_history = _history_inspection(turn, anchor.step) if anchor is not None else None
    current_history = _history_inspection(turn, model_step)
    if (
        anchor is None
        or anchor.usage.input_tokens is None
        or previous_history is None
        or current_history is None
        or current_history.history_items != len(history_documents)
        or _history_digest(history_documents) != current_history.view_history_sha256
        or previous_history.history_items > len(history_documents)
        or _history_digest(history_documents[: previous_history.history_items])
        != previous_history.view_history_sha256
    ):
        return full_estimate

    new_history_tokens = sum(
        estimate_tokens(document)
        for document in history_documents[previous_history.history_items :]
    )
    instruction_tokens = 0
    previous_context = _context_inspection(turn, anchor.step)
    current_context = _context_inspection(turn, model_step)
    if not _instructions_unchanged(previous_context, current_context, instructions):
        instruction_tokens = estimate_tokens(instructions or "")

    return (
        anchor.usage.input_tokens
        + new_history_tokens
        + sum(estimate_tokens(document) for document in tool_documents)
        + instruction_tokens
        + INCREMENTAL_REQUEST_OVERHEAD_TOKENS
    )


def estimate_request_input_tokens(
    provider: ModelProvider, request: ModelRequest, turn: Turn, legacy_estimate: int
) -> int:
    """旧 Provider 原样回退；新端口按出站输入估算，持久锚点必须同模型且前缀不变。"""
    if not isinstance(provider, ModelInputTokenEstimator):
        return legacy_estimate
    try:
        identity = provider.input_token_identity
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or any(type(part) is not str or not part for part in identity)
        ):
            raise ValueError("输入估算身份无效")
    except Exception:
        raise KernelError("invalid_provider_output", "Provider 输入预算身份无效") from None

    def wire_bound(*, prefix_items: int = 0) -> int:
        try:
            value = provider.estimate_input_tokens(request, prefix_items=prefix_items)
            if type(value) is not int or value <= 0:
                raise ValueError("输入估算必须是正整数")
            return value
        except Exception:
            raise KernelError("invalid_provider_output", "Provider 输入预算估算失败") from None

    wire = wire_bound()
    anchor = _previous_completed_attempt(turn, request.step)
    previous = _history_inspection(turn, anchor.step) if anchor is not None else None
    current = _history_inspection(turn, request.step)
    documents = tuple(history_document(item) for item in request.history)
    if (
        anchor is None
        or anchor.usage.input_tokens is None
        or identity != (anchor.provider, anchor.requested_model)
        or anchor.actual_model != anchor.requested_model
        or previous is None
        or current is None
        or current.history_items != len(documents)
        or _history_digest(documents) != current.view_history_sha256
        or previous.history_items > len(documents)
        or _history_digest(documents[: previous.history_items]) != previous.view_history_sha256
    ):
        return wire
    # 由 Provider 证明新增封装上界，不在 Runtime 中相减任意第三方的两个估算值。
    return min(wire, anchor.usage.input_tokens + wire_bound(prefix_items=previous.history_items))


def _tool_document(tool: ToolDescriptor) -> str:
    return json.dumps(
        tool.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _history_digest(documents: Sequence[str]) -> str:
    return hashlib.sha256(("[" + ",".join(documents) + "]").encode()).hexdigest()


def _previous_completed_attempt(turn: Turn, model_step: int) -> ModelAttempt | None:
    candidates = tuple(
        attempt
        for attempt in turn.model_attempts
        if attempt.step < model_step
        and attempt.status == "completed"
        and attempt.usage.completeness == "complete"
    )
    return candidates[-1] if candidates else None


def _history_inspection(turn: Turn, model_step: int) -> ModelHistoryInspectionRecord | None:
    return next(
        (
            inspection
            for inspection in reversed(turn.model_history_inspections)
            if inspection.model_step == model_step
        ),
        None,
    )


def _context_inspection(turn: Turn, model_step: int) -> ContextInspectionRecord | None:
    return next(
        (
            inspection
            for inspection in reversed(turn.context_inspections)
            if inspection.model_step == model_step
        ),
        None,
    )


def _instructions_unchanged(
    previous: ContextInspectionRecord | None,
    current: ContextInspectionRecord | None,
    instructions: str | None,
) -> bool:
    if previous is None or current is None:
        return previous is None and current is None and instructions is None
    return previous.instruction_fingerprint == current.instruction_fingerprint
