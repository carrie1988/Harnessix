"""模型Context规划：规划有预算约束的多步Context压缩。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TERMINAL_TURNS,
    Item,
    ItemStatus,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.context.compaction_contracts import (
    CompactionAnchor,
    CompactionPlan,
    CompactionPolicy,
    CompactionSummary,
)
from harnessix.context.compaction_ledger_contracts import COMPACTION_OPEN
from harnessix.context.compaction_projection import compaction_summary_item
from harnessix.context.compaction_window import prepare_active_model_history
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.context.tool_result_view import (
    PreparedModelHistory,
    history_document,
    history_items,
)


@dataclass(frozen=True, slots=True)
class PreparedCompaction:
    """只有规划证据；调用方仍须验证全部来源Artifact并持久化尝试。"""

    plan: CompactionPlan
    model_history: PreparedModelHistory = field(repr=False)
    summary_source: str = field(repr=False)
    retained_history: tuple[Item, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ValidatedCompaction:
    """内存中的候选投影，不表示摘要已计费结算或窗口已提交。"""

    plan: CompactionPlan
    summary: CompactionSummary = field(repr=False)
    history: tuple[Item, ...] = field(repr=False)
    history_sha256: str
    history_tokens: int
    summary_sha256: str


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _history_sha(items: tuple[Item, ...]) -> str:
    return _sha("[" + ",".join(history_document(item) for item in items) + "]")


async def _checkpoint(cancel: CancelToken) -> None:
    cancel.checkpoint()
    await asyncio.sleep(0)
    cancel.checkpoint()


def _closed_group_steps(
    history: tuple[Item, ...],
) -> Generator[None, None, tuple[tuple[Item, ...], ...]]:
    """保守合并连续助手块；只有用户边界或完整结果组结束才允许切分。"""
    groups: list[tuple[Item, ...]] = []
    current: list[Item] = []
    pending: set[UUID] = set()
    seen: set[UUID] = set()
    item_ids: set[UUID] = set()
    taking_results = False
    for item in history:
        yield None
        if item.item_id in item_ids or item.status != ItemStatus.COMPLETED:
            raise KernelError("context_compaction_invalid_history", "压缩来源Item重复或未完成")
        item_ids.add(item.item_id)
        content = item.content
        if isinstance(content, ToolResultContent):
            if content.call_id not in pending:
                raise KernelError("context_compaction_invalid_history", "结果缺少唯一前置调用")
            taking_results = True
            pending.remove(content.call_id)
            current.append(item)
            if not pending:
                groups.append(tuple(current))
                current = []
                taking_results = False
            continue
        if taking_results:
            raise KernelError("context_compaction_invalid_history", "结果组被其他消息打断")
        if isinstance(content, TextContent) and content.kind == "user_message":
            if pending:
                raise KernelError("context_compaction_invalid_history", "用户消息之前存在开放调用")
            if current:
                groups.append(tuple(current))
                current = []
            groups.append((item,))
        elif isinstance(content, TextContent) and content.kind == "assistant_message":
            current.append(item)
        elif isinstance(content, ToolCallContent):
            if content.call_id in seen:
                raise KernelError("context_compaction_invalid_history", "工具调用身份重复")
            seen.add(content.call_id)
            pending.add(content.call_id)
            current.append(item)
        else:
            raise KernelError("context_compaction_invalid_history", "压缩来源含未支持的消息类型")
    if pending:
        raise KernelError("context_compaction_invalid_history", "压缩来源存在未完成调用组")
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _current_user(thread: Thread, model_step: int) -> UUID:
    active = next((turn for turn in thread.turns if turn.turn_id == thread.active_turn_id), None)
    if (
        active is None
        or thread.sequence < 1
        or active.status != TurnStatus.PREPARING_CONTEXT
        or model_step != active.model_steps + 1
        or any(turn.status not in TERMINAL_TURNS for turn in thread.turns if turn is not active)
        or any(item.status == ItemStatus.STARTED for turn in thread.turns for item in turn.items)
        or any(a.status == "running" for turn in thread.turns for a in turn.accounted_attempts)
        or any(c.status in COMPACTION_OPEN for turn in thread.turns for c in turn.compactions)
    ):
        raise KernelError("context_compaction_unsafe_state", "压缩需要已结算、无开放Item的准备状态")
    if (
        active.model_steps >= active.budget.max_steps
        or active.usage.total_tokens >= active.budget.max_tokens
    ):
        raise KernelError("budget_exceeded", "压缩不能绕过Turn模型步骤或已知Token预算")
    users = [
        item.item_id
        for item in active.items
        if item.status == ItemStatus.COMPLETED
        and isinstance(item.content, TextContent)
        and item.content.kind == "user_message"
    ]
    if len(users) != 1:
        raise KernelError("context_compaction_invalid_history", "当前Turn缺少唯一用户消息")
    return users[0]


def _summary_source(items: tuple[Item, ...]) -> str:
    documents = []
    for item in items:
        content = item.content
        if isinstance(content, TextContent):
            data = content.model_dump(mode="json")
        elif isinstance(content, ToolCallContent):
            data = content.model_dump(mode="json", include={"kind", "call_id", "tool", "arguments"})
        else:
            assert isinstance(content, ToolResultContent)
            data = content.model_dump(
                mode="json",
                include={"kind", "call_id", "outcome", "output", "error", "diff_artifact"},
            )
        documents.append({"item_id": str(item.item_id), "content": data})
    return _json(
        {
            "schema": "harnessix.compaction-source/v1",
            "trust": "untrusted_history",
            "authority": "none",
            "items": documents,
        }
    )


def _plan_steps(
    thread: Thread,
    model_step: int,
    view_policy: ToolResultViewPolicy,
    policy: CompactionPolicy,
    *,
    compaction_id: UUID,
    anchors: tuple[CompactionAnchor, ...] = (),
) -> Generator[None, None, PreparedCompaction]:
    """规划首个压缩窗口；不调用Provider、读取Artifact或修改Session。"""
    yield None
    policy = CompactionPolicy.model_validate_json(policy.model_dump_json())
    view_policy = ToolResultViewPolicy.model_validate_json(view_policy.model_dump_json())
    current_user_id = _current_user(thread, model_step)
    if len(anchors) > 64 or len({anchor.item_id for anchor in anchors}) != len(anchors):
        raise KernelError("context_compaction_anchor_mismatch", "固定锚点超过上限或身份重复")
    anchors = tuple(
        CompactionAnchor.model_validate_json(anchor.model_dump_json()) for anchor in anchors
    )
    prepared = prepare_active_model_history(thread, model_step, view_policy)
    first = prepared.history[0]
    if not isinstance(first.content, TextContent) or first.content.kind != "user_message":
        raise KernelError("context_compaction_invalid_history", "历史缺少首条原用户消息")
    original = {item.item_id: item for item in history_items(thread)}
    # 现有Anthropic端口不接受assistant前缀；保留原消息而不伪造用户指令。
    requested_pins = {first.item_id, current_user_id}
    for anchor in anchors:
        item = original.get(anchor.item_id)
        if item is None or _sha(history_document(item)) != anchor.source_sha256:
            raise KernelError(
                "context_compaction_anchor_mismatch", "固定锚点与原始Session事实不匹配"
            )
        requested_pins.add(anchor.item_id)
    groups = yield from _closed_group_steps(prepared.history)
    pinned_groups = {
        index
        for index, group in enumerate(groups)
        if any(item.item_id in requested_pins for item in group)
    }
    sizes = [sum(len(history_document(item).encode()) for item in group) for group in groups]
    tail = max(0, len(groups) - policy.retain_recent_groups)
    selected = set(range(tail, len(groups))) | pinned_groups
    retained_tokens = sum(sizes[index] for index in selected)
    available = policy.target_history_tokens - policy.summary_reserve_tokens
    if retained_tokens > available:
        raise KernelError("context_compaction_retained_overflow", "固定项和近期完整组超过保留预算")
    for index in range(tail - 1, -1, -1):
        yield None
        if index in selected:
            continue
        if retained_tokens + sizes[index] > available:
            break
        selected.add(index)
        retained_tokens += sizes[index]
    covered = tuple(
        item for index, group in enumerate(groups) if index not in selected for item in group
    )
    retained = tuple(
        item for index, group in enumerate(groups) if index in selected for item in group
    )
    source_tokens = sum(sizes)
    if (
        not covered
        or source_tokens - retained_tokens - policy.summary_reserve_tokens
        < policy.min_savings_tokens
    ):
        raise KernelError("context_compaction_no_progress", "没有可压缩前缀或预留后不足最小缩减量")
    yield None
    summary_source = _summary_source(covered)
    summary_tokens = len(summary_source.encode())
    if summary_tokens > policy.max_summary_input_tokens:
        raise KernelError(
            "context_compaction_source_overflow", "摘要来源超过有效输入预算，不截断重试"
        )
    assert thread.active_turn_id is not None
    plan = CompactionPlan(
        compaction_id=compaction_id,
        thread_id=thread.thread_id,
        turn_id=thread.active_turn_id,
        source_event_sequence=thread.sequence,
        model_step=model_step,
        policy=policy,
        tool_result_view_policy=view_policy,
        anchors=anchors,
        source_item_ids=tuple(item.item_id for item in prepared.history),
        covered_item_ids=tuple(item.item_id for item in covered),
        retained_item_ids=tuple(item.item_id for item in retained),
        pinned_item_ids=tuple(
            item.item_id
            for index, group in enumerate(groups)
            if index in pinned_groups
            for item in group
        ),
        source_history_sha256=prepared.inspection.source_history_sha256,
        model_history_sha256=prepared.inspection.view_history_sha256,
        decisions_sha256=prepared.inspection.decisions_sha256,
        summary_source_sha256=_sha(summary_source),
        retained_history_sha256=_history_sha(retained),
        source_history_tokens=source_tokens,
        retained_history_tokens=retained_tokens,
        summary_input_tokens=summary_tokens,
    )
    yield None
    return PreparedCompaction(plan, prepared, summary_source, retained)


def _validation_steps(
    thread: Thread,
    plan: CompactionPlan,
    summary: CompactionSummary,
) -> Generator[None, None, ValidatedCompaction]:
    """重新求证快照及预算；调用方仍须完成尝试绑定和Session CAS发布。"""
    yield None
    try:
        plan = CompactionPlan.model_validate_json(plan.model_dump_json())
        summary = CompactionSummary.model_validate_json(summary.model_dump_json())
    except ValueError:
        raise KernelError(
            "context_compaction_candidate_invalid", "压缩计划或摘要不符合契约"
        ) from None
    if (
        summary.compaction_id != plan.compaction_id
        or thread.thread_id != plan.thread_id
        or thread.active_turn_id != plan.turn_id
        or thread.sequence != plan.source_event_sequence
    ):
        raise KernelError("context_compaction_source_changed", "候选与来源快照身份不一致")
    prepared = yield from _plan_steps(
        thread,
        plan.model_step,
        plan.tool_result_view_policy,
        plan.policy,
        compaction_id=plan.compaction_id,
        anchors=plan.anchors,
    )
    if prepared.plan != plan:
        raise KernelError("context_compaction_source_changed", "候选来源或选择证据发生变化")
    try:
        summary_item = compaction_summary_item(summary)
    except ValueError:
        raise KernelError(
            "context_compaction_summary_overflow", "摘要完整投影超过文本保护上限"
        ) from None
    if any(summary_item.item_id == item.item_id for turn in thread.turns for item in turn.items):
        raise KernelError("context_compaction_candidate_invalid", "摘要投影身份与原Item冲突")
    summary_tokens = len(history_document(summary_item).encode())
    if summary_tokens > plan.policy.summary_reserve_tokens:
        raise KernelError("context_compaction_summary_overflow", "摘要完整投影超过预留预算")
    history = (prepared.retained_history[0], summary_item, *prepared.retained_history[1:])
    after_tokens = summary_tokens + plan.retained_history_tokens
    if (
        after_tokens > plan.policy.target_history_tokens
        or plan.source_history_tokens - after_tokens < plan.policy.min_savings_tokens
    ):
        raise KernelError("context_compaction_no_progress", "候选窗口未达到目标预算和缩减量")
    yield from _closed_group_steps(history)
    return ValidatedCompaction(
        plan=plan,
        summary=summary,
        history=history,
        history_sha256=_history_sha(history),
        history_tokens=after_tokens,
        summary_sha256=_sha(summary.text),
    )


def _collect[T](steps: Generator[None, None, T]) -> T:
    try:
        while True:
            try:
                next(steps)
            except StopIteration as finished:
                return cast(T, finished.value)
    finally:
        steps.close()


async def _iterate[T](steps: Generator[None, None, T], cancel: CancelToken) -> T:
    try:
        while True:
            await _checkpoint(cancel)
            try:
                next(steps)
            except StopIteration as finished:
                return cast(T, finished.value)
    finally:
        steps.close()


def replay_compaction_plan(thread: Thread, plan: CompactionPlan) -> PreparedCompaction:
    prepared = _collect(
        _plan_steps(
            thread,
            plan.model_step,
            plan.tool_result_view_policy,
            plan.policy,
            compaction_id=plan.compaction_id,
            anchors=plan.anchors,
        )
    )
    if prepared.plan != plan:
        raise KernelError("context_compaction_source_changed", "压缩计划与原快照重算不一致")
    return prepared


def replay_compaction_candidate(
    thread: Thread,
    plan: CompactionPlan,
    summary: CompactionSummary,
) -> ValidatedCompaction:
    return _collect(_validation_steps(thread, plan, summary))


async def plan_compaction(
    thread: Thread,
    model_step: int,
    view_policy: ToolResultViewPolicy,
    policy: CompactionPolicy,
    cancel: CancelToken,
    *,
    compaction_id: UUID,
    anchors: tuple[CompactionAnchor, ...] = (),
) -> PreparedCompaction:
    """可取消规划与同步Replay共用有界纯计算，不执行I/O。"""
    return await _iterate(
        _plan_steps(
            thread,
            model_step,
            view_policy,
            policy,
            compaction_id=compaction_id,
            anchors=anchors,
        ),
        cancel,
    )


async def validate_compaction(
    thread: Thread,
    plan: CompactionPlan,
    summary: CompactionSummary,
    cancel: CancelToken,
) -> ValidatedCompaction:
    return await _iterate(_validation_steps(thread, plan, summary), cancel)
