"""系统适配器提供的中立逐步记录；evalkit只编码和关联，不解释系统内部状态。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from evalkit.contracts import Contract, Identifier, RefKey, ShortText, Usage

STEP_TEXT_LIMIT = 1_000_000


def step_text(blocks: Iterable[tuple[str, str]]) -> str | None:
    """各系统共用的步骤正文格式：每段以“[标签]”起头，段间空行；超过上限截断并注明截掉的字符数。

    模型调用的输入只含上次调用之后新增的内容（首次为任务），输出含回复与发出的工具调用，
    两个系统按同一口径拼接，Langfuse中同类步骤可直接对照。
    """
    text = "\n\n".join(f"[{label}]\n{body}" for label, body in blocks if body)
    if not text:
        return None
    if len(text) <= STEP_TEXT_LIMIT:
        return text
    keep = STEP_TEXT_LIMIT - len(f"\n…[truncated {len(text)} chars]")  # 按位数上界预留标注
    return text[:keep] + f"\n…[truncated {len(text) - keep} chars]"


class Step(Contract):
    """一次模型调用、工具调用或其他步骤；正文只在计划声明完整内容时出现。"""

    step_id: Identifier = Field(description="系统内的稳定事件ID，重发时保持不变")
    parent_step_id: Identifier | None = None
    kind: Literal["generation", "tool", "span"]
    name: ShortText
    started_at: AwareDatetime
    completed_at: AwareDatetime
    status: Literal["ok", "error"]
    model: ShortText | None = None
    usage: Usage | None = None
    input_text: str | None = Field(default=None, max_length=1_000_000)
    output_text: str | None = Field(default=None, max_length=1_000_000)
    metadata: dict[RefKey, ShortText] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def window_is_ordered(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("步骤时间倒置")
        if self.kind != "generation" and (self.model is not None or self.usage is not None):
            raise ValueError("只有模型调用步骤携带模型与用量")
        return self


class StepTrace(Contract):
    """一个试次的全部步骤，按开始时间排序。"""

    plan_digest: Identifier
    slot: int = Field(ge=0)
    steps: tuple[Step, ...] = Field(max_length=20_000)

    @model_validator(mode="after")
    def steps_are_consistent(self) -> Self:
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("步骤ID重复")
        known = set(ids)
        if any(s.parent_step_id is not None and s.parent_step_id not in known for s in self.steps):
            raise ValueError("父步骤不在同一试次内")
        if list(self.steps) != sorted(self.steps, key=lambda s: s.started_at):
            raise ValueError("步骤必须按开始时间排序")
        return self

    def without_content(self) -> StepTrace:
        """元数据级别导出：去掉全部正文。"""
        return self.model_copy(
            update={
                "steps": tuple(
                    s.model_copy(update={"input_text": None, "output_text": None})
                    for s in self.steps
                )
            }
        )
