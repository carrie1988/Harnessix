"""重复模型请求的保守 Turn Token 预算估算。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from harnessix.agent.models import Item, Turn
from harnessix.agent.usage import ModelAttempt
from harnessix.context.contracts import ContextInspectionRecord
from harnessix.context.engine import estimate_tokens
from harnessix.context.tool_result_contracts import ModelHistoryInspectionRecord
from harnessix.context.tool_result_view import history_document
from harnessix.domain.models import ToolDescriptor

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
