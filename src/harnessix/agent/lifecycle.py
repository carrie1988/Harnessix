"""持久Agent状态机：定义Thread生命周期与Fork快照校验。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ForkArtifactOwner,
    Item,
    Thread,
    ThreadForkSnapshot,
    ToolResultContent,
    TurnStatus,
)
from harnessix.context.compaction_window import active_model_history_source
from harnessix.context.tool_result_contracts import ToolResultViewDecision, ToolResultViewPolicy
from harnessix.context.tool_result_view import (
    PreparedModelHistory,
    history_items,
    prepare_model_history_items,
)


@dataclass(frozen=True, slots=True)
class PreparedForkSnapshot:
    snapshot: ThreadForkSnapshot
    model_history: PreparedModelHistory | None


def thread_sha256(thread: Thread) -> str:
    return hashlib.sha256(_canonical(thread.model_dump(mode="json"))).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, UnicodeError):
        raise KernelError("thread_fork_invalid", "Fork来源不能规范化为有限值UTF-8 JSON") from None


def _history_sha256(items: tuple[Item, ...]) -> str:
    return hashlib.sha256(_canonical([item.model_dump(mode="json") for item in items])).hexdigest()


def _source_history(thread: Thread, through_turn_id: UUID | None) -> tuple[Item, ...]:
    if thread.active_turn_id is not None:
        raise KernelError("thread_busy", "活跃Turn结束前不能Fork Thread")
    if thread.archive is not None:
        raise KernelError("thread_archived", "归档Thread不能作为Fork来源")
    if through_turn_id is None:
        if thread.turns:
            through_turn_id = thread.turns[-1].turn_id
        elif thread.fork_snapshot is not None:
            return thread.fork_snapshot.items
        else:
            return ()
    index = next(
        (index for index, turn in enumerate(thread.turns) if turn.turn_id == through_turn_id),
        None,
    )
    if index is None:
        inherited = thread.fork_snapshot
        if inherited is not None and through_turn_id == inherited.through_turn_id:
            return inherited.items
        raise KernelError("turn_not_found", "Fork边界Turn不存在")
    turn = thread.turns[index]
    if turn.status not in {
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.CANCELLED,
        TurnStatus.INTERRUPTED,
    }:
        raise KernelError("thread_busy", "Fork边界Turn尚未终结")
    if index == len(thread.turns) - 1:
        return active_model_history_source(thread)
    prefix = thread.model_copy(
        update={
            "turns": thread.turns[: index + 1],
            "compaction_windows": (),
            "active_compaction_window_id": None,
        },
        deep=True,
    )
    return history_items(prefix)


def _existing_decisions(thread: Thread) -> dict[UUID, ToolResultViewDecision]:
    decisions = [
        *(thread.fork_snapshot.tool_result_view_decisions if thread.fork_snapshot else ()),
        *(decision for turn in thread.turns for decision in turn.tool_result_view_decisions),
    ]
    by_item = {decision.item_id: decision for decision in decisions}
    if len(by_item) != len(decisions):
        raise KernelError("thread_fork_invalid", "Fork来源模型视图决定身份重复")
    return by_item


def _ordered_decisions(
    thread: Thread,
    source: tuple[Item, ...],
    created: tuple[ToolResultViewDecision, ...],
) -> tuple[ToolResultViewDecision, ...]:
    by_item = _existing_decisions(thread) | {decision.item_id: decision for decision in created}
    result_ids = [item.item_id for item in source if isinstance(item.content, ToolResultContent)]
    try:
        return tuple(by_item[item_id] for item_id in result_ids)
    except KeyError:
        raise KernelError("thread_fork_invalid", "Fork来源Tool Result缺少冻结模型视图") from None


def _owner_by_artifact(thread: Thread) -> dict[UUID, UUID]:
    if thread.fork_snapshot is None:
        return {}
    return {
        owner.artifact_id: owner.owner_thread_id for owner in thread.fork_snapshot.artifact_owners
    }


def _artifact_owners(
    thread: Thread, prepared: PreparedModelHistory
) -> tuple[ForkArtifactOwner, ...]:
    inherited = _owner_by_artifact(thread)
    found: dict[UUID, UUID] = {}
    for reference in prepared.references:
        artifact_id = reference.binding.artifact.artifact_id
        owner = reference.owner_thread_id or inherited.get(artifact_id) or thread.thread_id
        if artifact_id in found and found[artifact_id] != owner:
            raise KernelError("thread_fork_invalid", "同一Artifact出现多个历史所有者")
        found[artifact_id] = owner
    return tuple(
        ForkArtifactOwner(artifact_id=artifact_id, owner_thread_id=owner)
        for artifact_id, owner in found.items()
    )


def prepare_fork_snapshot(
    thread: Thread,
    *,
    request_id: str,
    through_turn_id: UUID | None,
    policy: ToolResultViewPolicy,
) -> PreparedForkSnapshot:
    source = _source_history(thread, through_turn_id)
    effective_turn_id = through_turn_id
    if effective_turn_id is None:
        if thread.turns:
            effective_turn_id = thread.turns[-1].turn_id
        elif thread.fork_snapshot is not None:
            effective_turn_id = thread.fork_snapshot.through_turn_id
    if not source:
        digest = _history_sha256(())
        snapshot = ThreadForkSnapshot(
            request_id=request_id,
            source_thread_id=thread.thread_id,
            source_sequence=thread.sequence,
            through_turn_id=effective_turn_id,
            source_compaction_window_id=None,
            source_thread_sha256=thread_sha256(thread),
            source_history_sha256=digest,
            view_history_sha256=digest,
            tool_result_view_policy=policy,
        )
        return PreparedForkSnapshot(snapshot=snapshot, model_history=None)
    prepared = prepare_model_history_items(thread, source, 1, policy)
    decisions = _ordered_decisions(thread, source, prepared.new_decisions)
    snapshot = ThreadForkSnapshot(
        request_id=request_id,
        source_thread_id=thread.thread_id,
        source_sequence=thread.sequence,
        through_turn_id=effective_turn_id,
        source_compaction_window_id=(
            thread.active_compaction_window_id
            if thread.turns and effective_turn_id == thread.turns[-1].turn_id
            else None
        ),
        source_thread_sha256=thread_sha256(thread),
        source_history_sha256=_history_sha256(source),
        view_history_sha256=prepared.inspection.view_history_sha256,
        tool_result_view_policy=policy,
        items=source,
        tool_result_view_decisions=decisions,
        artifact_owners=_artifact_owners(thread, prepared),
    )
    return PreparedForkSnapshot(snapshot=snapshot, model_history=prepared)


def validate_fork_snapshot(
    source: Thread, snapshot: ThreadForkSnapshot
) -> PreparedModelHistory | None:
    if (
        snapshot.source_thread_id != source.thread_id
        or snapshot.source_sequence != source.sequence
        or snapshot.source_thread_sha256 != thread_sha256(source)
    ):
        raise KernelError("sequence_conflict", "Fork来源Thread已变化")
    expected = prepare_fork_snapshot(
        source,
        request_id=snapshot.request_id,
        through_turn_id=snapshot.through_turn_id,
        policy=snapshot.tool_result_view_policy,
    )
    if expected.snapshot != snapshot:
        raise KernelError("thread_fork_invalid", "Fork快照不能由来源Thread确定性重建")
    return expected.model_history
