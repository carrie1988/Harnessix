"""只读结果随存档提交的事件批次：带存档引用，或按原因不带存档（ADR 0163、0174）。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    ToolResultContent,
)
from harnessix.artifacts.contracts import ARTIFACT_OMISSIONS, ARTIFACT_OMITTED_KEY, ArtifactRef

type ResultBatch = tuple[EventDraft, ...]
type BatchFreezer = Callable[[Sequence[EventDraft]], ResultBatch]


def result_batches(
    freeze: BatchFreezer,
    turn_id: UUID,
    result: ToolResultContent,
    ref: ArtifactRef,
    max_output_chars: int,
) -> tuple[ResultBatch, dict[str, ResultBatch]]:
    """带存档引用的结果，以及每种不保存原因下照常提交的结果；都在事务前核对输出上限。"""

    def batch(archive: dict[str, JsonValue]) -> ResultBatch:
        content = result.model_copy(update={"output": {"preview": result.output, **archive}})
        if len(content.model_dump_json()) > max_output_chars:
            raise KernelError("tool_output_too_large", "Artifact 引用与预览超过 Kernel 上限")
        item_id = new_id()
        started = ItemStarted(item_id=item_id, content=content)
        finished = ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED)
        return freeze(
            (
                EventDraft(turn_id=turn_id, payload=started),
                EventDraft(turn_id=turn_id, payload=finished),
            )
        )

    omitted = {reason: batch({ARTIFACT_OMITTED_KEY: reason}) for reason in ARTIFACT_OMISSIONS}
    return batch({"artifact": ref.model_dump(mode="json")}), omitted
