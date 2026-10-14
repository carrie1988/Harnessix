"""模型Context规划：只保留最近工具调用组的结果正文，较旧结果换成固定占位。

工作集是模型视图上的确定性投影：由已准备的基础视图与保留组数唯一决定，回放可重算；
Native事实、Tool Result决定与Artifact均不改变。被省略的正文需要时由模型重新取证。
"""

from __future__ import annotations

from dataclasses import replace

from harnessix.agent.models import (
    Item,
    TextContent,
    Thread,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
)
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.tool_result_contracts import (
    STALE_TOOL_RESULT_VERSION,
    ModelHistoryInspection,
    ModelHistoryInspectionRecord,
    ModelHistoryInspectionV3,
    ToolResultViewDecision,
    ToolResultViewPolicy,
)
from harnessix.context.tool_result_view import (
    PreparedModelHistory,
    canonical_json_bytes,
    history_items,
    prepare_model_history_items,
    view_history_sha256,
)

# 占位只携带短标量（路径、行号、revision、摘要等），不复制任何长正文。
_MAX_RETAINED_SCALAR_BYTES = 160


def prepare_model_view(
    thread: Thread,
    model_step: int,
    policy: ToolResultViewPolicy,
    retain_recent_result_groups: int | None,
    *,
    decisions: tuple[ToolResultViewDecision, ...] | None = None,
) -> PreparedModelHistory:
    """未启用工作集时与原活动历史完全一致；活动摘要窗口内不叠加遮蔽。"""
    prepared = prepare_active_model_history(thread, model_step, policy, decisions=decisions)
    if retain_recent_result_groups is None or not isinstance(
        prepared.inspection, ModelHistoryInspection
    ):
        return prepared
    return apply_working_set(prepared, retain_recent_result_groups)


def previous_model_view(
    thread: Thread, inspection: ModelHistoryInspectionRecord
) -> tuple[Item, ...] | None:
    """按持久检查记录重算某一步实际发送的历史；无法逐字节复现时返回 None。"""
    if not isinstance(inspection, ModelHistoryInspection | ModelHistoryInspectionV3):
        return None
    source = history_items(thread)[: inspection.history_items]
    if len(source) != inspection.history_items:
        return None
    prepared = prepare_model_history_items(thread, source, inspection.model_step, inspection.policy)
    if prepared.new_decisions:
        return None
    if isinstance(inspection, ModelHistoryInspectionV3):
        prepared = apply_working_set(prepared, inspection.retain_recent_result_groups)
    if prepared.inspection != inspection:
        return None
    return prepared.history


def apply_working_set(
    prepared: PreparedModelHistory, retain_recent_result_groups: int
) -> PreparedModelHistory:
    base = prepared.inspection
    if not isinstance(base, ModelHistoryInspection):
        raise ValueError("工作集只叠加在无摘要窗口的基础视图上")
    stale_calls = _stale_call_ids(prepared.history, retain_recent_result_groups)
    history: list[Item] = []
    stale_results = 0
    for item in prepared.history:
        content = item.content
        if isinstance(content, ToolResultContent) and content.call_id in stale_calls:
            placeholder = _stale_output(content.output)
            if len(canonical_json_bytes(placeholder)) < len(canonical_json_bytes(content.output)):
                item = item.model_copy(
                    update={"content": content.model_copy(update={"output": placeholder})}
                )
                stale_results += 1
        history.append(item)
    view = tuple(history)
    inspection = ModelHistoryInspectionV3(
        **base.model_dump(exclude={"spec_version", "view_history_sha256"}),
        view_history_sha256=view_history_sha256(view),
        retain_recent_result_groups=retain_recent_result_groups,
        stale_results=stale_results,
    )
    return replace(prepared, history=view, inspection=inspection)


def _stale_call_ids(history: tuple[Item, ...], retain: int) -> frozenset[object]:
    """调用组是一次模型响应发出的全部工具调用；保留最近 retain 组，拒绝调用结果不遮蔽。"""
    groups: list[list[object]] = []
    in_response = False
    for item in history:
        content = item.content
        responding = isinstance(content, ToolCallContent | ToolCallRejectionContent) or (
            isinstance(content, TextContent) and content.kind == "assistant_message"
        )
        if responding and not in_response:
            groups.append([])
        in_response = responding
        if isinstance(content, ToolCallContent):
            groups[-1].append(content.call_id)
    called = [group for group in groups if group]
    return frozenset(call for group in called[: max(0, len(called) - retain)] for call in group)


def _stale_output(output: object) -> dict[str, object]:
    placeholder: dict[str, object] = {
        "spec_version": STALE_TOOL_RESULT_VERSION,
        "source_utf8_bytes": len(canonical_json_bytes(output)),
        "note": "Older tool output omitted from this view; repeat a focused call if needed.",
    }
    if isinstance(output, dict):
        retained = {
            key: value
            for key, value in sorted(output.items())
            if value is None
            or isinstance(value, bool | int | float)
            or (
                isinstance(value, str)
                and len(value.encode()) <= _MAX_RETAINED_SCALAR_BYTES
                and "\n" not in value
            )
        }
        if retained:
            placeholder["retained"] = retained
    return placeholder
