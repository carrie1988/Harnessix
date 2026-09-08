from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import CompactionWindow, Item, Thread
from harnessix.context.compaction_ledger_contracts import CompactionRecord
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.tool_result_contracts import (
    ModelHistoryInspectionV2,
    ToolResultViewDecision,
    ToolResultViewPolicy,
)
from harnessix.context.tool_result_view import (
    PreparedModelHistory,
    history_document,
    history_items,
    prepare_model_history_items,
)

if TYPE_CHECKING:
    from harnessix.context.compaction import ValidatedCompaction


def _digest_ids(items: tuple[Item, ...]) -> str:
    body = json.dumps(
        [str(item.item_id) for item in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _history_digest(items: tuple[Item, ...]) -> str:
    body = "[" + ",".join(history_document(item) for item in items) + "]"
    return hashlib.sha256(body.encode()).hexdigest()


def compaction_record(thread: Thread, compaction_id: UUID) -> CompactionRecord:
    records = [
        record
        for turn in thread.turns
        for record in turn.compactions
        if record.plan.compaction_id == compaction_id
    ]
    if len(records) != 1:
        raise KernelError("context_compaction_window_invalid", "窗口缺少唯一摘要账本")
    return records[0]


def active_window(thread: Thread) -> CompactionWindow | None:
    identity = thread.active_compaction_window_id
    if identity is None:
        return None
    matches = [window for window in thread.compaction_windows if window.window_id == identity]
    if len(matches) != 1 or thread.compaction_windows[-1] != matches[0]:
        raise KernelError("context_compaction_window_invalid", "活动窗口不属于线性链尾")
    return matches[0]


def build_compaction_window(
    thread: Thread,
    record: CompactionRecord,
    candidate: ValidatedCompaction,
    *,
    window_id: UUID,
    activated_event_sequence: int,
    activated_at: datetime,
) -> CompactionWindow:
    raw = history_items(thread)
    if not raw or record.finished_event_sequence is None:
        raise KernelError("context_compaction_window_invalid", "窗口缺少原历史或候选序号")
    return CompactionWindow(
        window_id=window_id,
        compaction_id=record.plan.compaction_id,
        previous_window_id=thread.active_compaction_window_id,
        source_finished_event_sequence=record.finished_event_sequence,
        activated_event_sequence=activated_event_sequence,
        model_step=record.plan.model_step,
        history_item_ids=tuple(item.item_id for item in candidate.history),
        history_sha256=candidate.history_sha256,
        history_tokens=candidate.history_tokens,
        raw_history_items=len(raw),
        raw_history_last_item_id=raw[-1].item_id,
        raw_history_ids_sha256=_digest_ids(raw),
        activated_at=activated_at,
    )


def _window_source(thread: Thread, window: CompactionWindow) -> tuple[Item, ...]:
    raw = history_items(thread)
    if (
        len(raw) < window.raw_history_items
        or raw[window.raw_history_items - 1].item_id != window.raw_history_last_item_id
        or _digest_ids(raw[: window.raw_history_items]) != window.raw_history_ids_sha256
    ):
        raise KernelError("context_compaction_window_source_changed", "活动窗口原历史前缀发生变化")
    by_id = {item.item_id: item for item in raw}
    for turn in thread.turns:
        for record in turn.compactions:
            if record.status != "summarized" or record.summary is None:
                continue
            item = compaction_summary_item(record.summary)
            if item.item_id in by_id:
                raise KernelError("context_compaction_window_invalid", "摘要身份与原Item冲突")
            by_id[item.item_id] = item
    try:
        base = tuple(by_id[item_id] for item_id in window.history_item_ids)
    except KeyError:
        raise KernelError("context_compaction_window_invalid", "窗口引用不存在的Item") from None
    if len({item.item_id for item in base}) != len(base):
        raise KernelError("context_compaction_window_invalid", "窗口Item身份重复")
    record = compaction_record(thread, window.compaction_id)
    prepared = prepare_model_history_items(
        thread,
        base,
        window.model_step,
        record.plan.tool_result_view_policy,
        require_all_prior_decisions=False,
    )
    if (
        prepared.new_decisions
        or _history_digest(prepared.history) != window.history_sha256
        or sum(len(history_document(item).encode()) for item in prepared.history)
        != window.history_tokens
    ):
        raise KernelError("context_compaction_window_invalid", "活动窗口与摘要候选证据不一致")
    return (*base, *raw[window.raw_history_items :])


def prepare_active_model_history(
    thread: Thread,
    model_step: int,
    policy: ToolResultViewPolicy,
    *,
    decisions: tuple[ToolResultViewDecision, ...] | None = None,
) -> PreparedModelHistory:
    window = active_window(thread)
    if window is None:
        from harnessix.context.tool_result_view import prepare_model_history

        return prepare_model_history(thread, model_step, policy, decisions=decisions)
    source = _window_source(thread, window)
    prepared = prepare_model_history_items(
        thread,
        source,
        model_step,
        policy,
        decisions=decisions,
        require_all_prior_decisions=False,
    )
    inspection = ModelHistoryInspectionV2(
        **prepared.inspection.model_dump(exclude={"spec_version"}),
        window_id=window.window_id,
        window_history_sha256=window.history_sha256,
        raw_history_items=len(history_items(thread)),
    )
    return PreparedModelHistory(
        history=prepared.history,
        inspection=inspection,
        new_decisions=prepared.new_decisions,
        references=prepared.references,
    )
