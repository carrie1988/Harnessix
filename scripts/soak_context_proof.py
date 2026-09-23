"""长会话Soak的低敏Context/Compaction投影证明合同。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from harnessix.domain.models import ContractModel

CONTEXT_PROOF_FILENAME = "context-proof.json"
MAX_CONTEXT_PROOF_BYTES = 4 * 1024 * 1024

_COMPACTION_SEQUENCE = (
    "compaction_planned",
    "compaction_attempt_started",
    "compaction_usage_observed",
    "compaction_attempt_finished",
    "compaction_summarized",
    "compaction_window_activated",
)


class SoakEventMarker(ContractModel):
    """仅保留事件序号、类型和Turn序号，拒绝业务身份与正文。"""

    sequence: StrictInt = Field(ge=1)
    turn_ordinal: StrictInt = Field(ge=0, le=11_000)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class SoakContextTurn(ContractModel):
    """按原始Turn顺序保存计数，不保存Thread、Turn或Window身份。"""

    ordinal: StrictInt = Field(ge=1, le=11_000)
    phase: Literal["warmup", "measure"]
    model_steps: StrictInt = Field(ge=1, le=1000)
    context_inspections: StrictInt = Field(ge=1, le=1000)
    history_inspections: StrictInt = Field(ge=1, le=1000)
    compaction_plans: StrictInt = Field(ge=0, le=1000)
    summary_attempts: StrictInt = Field(ge=0, le=1000)
    completed_summaries: StrictInt = Field(ge=0, le=1000)
    window_activations: StrictInt = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def complete_turn(self) -> Self:
        if (
            self.context_inspections != self.model_steps
            or self.history_inspections != self.model_steps
            or len(
                {
                    self.compaction_plans,
                    self.summary_attempts,
                    self.completed_summaries,
                    self.window_activations,
                }
            )
            != 1
        ):
            raise ValueError("长会话Context检查、摘要账本或窗口数量不一致")
        return self


class SoakContextProof(ContractModel):
    """从已重放Session投影派生；Reader可独立复核覆盖数量与窗口链长度。"""

    spec_version: Literal["harnessix.soak-context-proof/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    turns: tuple[SoakContextTurn, ...] = Field(min_length=1, max_length=11_000)
    events: tuple[SoakEventMarker, ...] = Field(min_length=1, max_length=100_000)
    final_window_count: StrictInt = Field(ge=1, le=1000)
    final_event_sequence: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def continuous_and_covered(self) -> Self:
        if any(turn.ordinal != index for index, turn in enumerate(self.turns, start=1)):
            raise ValueError("长会话证明Turn序号不连续")
        measured = [turn for turn in self.turns if turn.phase == "measure"]
        if (
            not measured
            or sum(turn.completed_summaries for turn in measured) < 1
            or sum(turn.window_activations for turn in self.turns) != self.final_window_count
            or len(self.events) != self.final_event_sequence
            or any(marker.sequence != index for index, marker in enumerate(self.events, start=1))
        ):
            raise ValueError("长会话证明缺少正式压缩或窗口链")
        by_turn: dict[int, list[str]] = {turn.ordinal: [] for turn in self.turns}
        for marker in self.events:
            if marker.turn_ordinal > 0:
                if marker.turn_ordinal not in by_turn:
                    raise ValueError("长会话证明引用未知Turn序号")
                by_turn[marker.turn_ordinal].append(marker.kind)
            elif marker.kind in _COMPACTION_SEQUENCE or marker.kind in {
                "context_prepared",
                "compaction_rejected",
            }:
                raise ValueError("Context与压缩事件缺少Turn归属")
        for turn in self.turns:
            kinds = by_turn[turn.ordinal]
            if (
                kinds.count("context_prepared") != turn.context_inspections
                or kinds.count("model_history_prepared") != turn.history_inspections
                or kinds.count("compaction_rejected")
                or [kind for kind in kinds if kind in _COMPACTION_SEQUENCE]
                != list(_COMPACTION_SEQUENCE) * turn.completed_summaries
            ):
                raise ValueError("长会话证明事件与Turn投影不一致")
        if sum(marker.kind == "compaction_window_activated" for marker in self.events) != (
            self.final_window_count
        ):
            raise ValueError("长会话证明窗口事件数量不一致")
        return self

    @property
    def model_request_count(self) -> int:
        return sum(turn.model_steps for turn in self.turns)

    @property
    def summary_request_count(self) -> int:
        return sum(turn.summary_attempts for turn in self.turns)
