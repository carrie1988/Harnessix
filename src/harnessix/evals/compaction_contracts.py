"""Compaction 关键工程语义保持评测的严格契约。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.evals.contracts import EvalContract

CompactionSemanticCategory = Literal[
    "objective",
    "constraint",
    "unresolved_work",
    "file_revision",
    "test_result",
    "effect_uncertainty",
]
COMPACTION_SEMANTIC_CATEGORIES: tuple[CompactionSemanticCategory, ...] = (
    "objective",
    "constraint",
    "unresolved_work",
    "file_revision",
    "test_result",
    "effect_uncertainty",
)


def _phrases(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    if list(values) != sorted(set(values)):
        raise ValueError(f"{name}必须唯一并按字节序排序")
    for value in values:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise ValueError(f"{name}必须是合法UTF-8") from None
        if not value.strip() or len(encoded) > 1024 or "\x00" in value:
            raise ValueError(f"{name}为空、过长或包含NUL")
    return values


class CompactionSemanticExpectation(EvalContract):
    """由评测语料人工绑定的来源事实及可接受等价表述。"""

    expectation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    category: CompactionSemanticCategory
    source_markers: tuple[str, ...] = Field(min_length=1, max_length=8)
    accepted_summary_phrases: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("source_markers", "accepted_summary_phrases")
    @classmethod
    def valid_phrases(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        name = getattr(info, "field_name", "评测短语")
        return _phrases(value, name)


class CompactionSemanticEvalCase(EvalContract):
    """覆盖全部关键类别的版本化语义保持评测用例。"""

    spec_version: Literal["harnessix.compaction-semantic-eval-case/v1"] = (
        "harnessix.compaction-semantic-eval-case/v1"
    )
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    expectations: tuple[CompactionSemanticExpectation, ...] = Field(min_length=6, max_length=64)
    forbidden_summary_phrases: tuple[str, ...] = Field(min_length=1, max_length=32)
    omitted_history_markers: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("forbidden_summary_phrases", "omitted_history_markers")
    @classmethod
    def valid_phrases(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        name = getattr(info, "field_name", "评测短语")
        return _phrases(value, name)

    @model_validator(mode="after")
    def complete_oracle(self) -> Self:
        identities = [expectation.expectation_id for expectation in self.expectations]
        if identities != sorted(set(identities)):
            raise ValueError("语义期望身份必须唯一并按字节序排序")
        if {expectation.category for expectation in self.expectations} != set(
            COMPACTION_SEMANTIC_CATEGORIES
        ):
            raise ValueError("语义评测必须覆盖全部关键工程语义类别")
        return self


class CompactionSemanticCheck(EvalContract):
    expectation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    category: CompactionSemanticCategory
    source_bound: bool
    preserved: bool


class CompactionSemanticEvalReport(EvalContract):
    """不保存来源或摘要正文的确定性评测报告。"""

    spec_version: Literal["harnessix.compaction-semantic-eval-report/v1"] = (
        "harnessix.compaction-semantic-eval-report/v1"
    )
    case_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    compaction_id: UUID
    outcome: Literal["passed", "failed", "invalid"]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    history_tokens: int = Field(ge=1, le=8_388_608, strict=True)
    corpus_bound: bool
    forbidden_summary_clean: bool
    covered_history_omitted: bool
    checks: tuple[CompactionSemanticCheck, ...] = Field(min_length=6, max_length=64)
    failed_expectation_ids: tuple[str, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def result_is_consistent(self) -> Self:
        identities = [check.expectation_id for check in self.checks]
        if identities != sorted(set(identities)):
            raise ValueError("语义检查身份必须唯一并按字节序排序")
        failed = tuple(check.expectation_id for check in self.checks if not check.preserved)
        if self.failed_expectation_ids != failed:
            raise ValueError("语义失败集合与检查结果不一致")
        expected = (
            "invalid"
            if not self.corpus_bound
            else "passed"
            if not failed and self.forbidden_summary_clean and self.covered_history_omitted
            else "failed"
        )
        if self.outcome != expected:
            raise ValueError("语义评测结论与证据不一致")
        return self
