from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from harnessix.domain.errors import HarnessixError
from harnessix.domain.models import ContractModel


class FailureCategory(StrEnum):
    INPUT = "input"
    PROVIDER = "provider"
    TOOL = "tool"
    APPROVAL = "approval"
    BUDGET = "budget"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    STORAGE = "storage"
    CONFLICT = "conflict"
    INTERNAL = "internal"


def failure_category(code: str) -> FailureCategory:
    if code in {"cancelled", "provider_cancelled"}:
        return FailureCategory.CANCELLED
    if code in {"process_interrupted", "uncertain_effect"}:
        return FailureCategory.INTERRUPTED
    if code in {
        "budget_exceeded",
        "time_budget_exceeded",
        "model_output_too_large",
        "tool_output_too_large",
    }:
        return FailureCategory.BUDGET
    if code in {
        "request_conflict",
        "sequence_conflict",
        "event_conflict",
        "runtime_busy",
        "turn_busy",
    }:
        return FailureCategory.CONFLICT
    if code.startswith(
        ("storage_", "projection_", "event_corrupt", "database_", "schema_", "migration_")
    ) or code in {"wrong_database", "invalid_migration"}:
        return FailureCategory.STORAGE
    if code.startswith("approval_"):
        return FailureCategory.APPROVAL
    if code.startswith("provider_") or code == "invalid_provider_output":
        return FailureCategory.PROVIDER
    if code.startswith("tool_") or code in {"unknown_tool", "duplicate_tool"}:
        return FailureCategory.TOOL
    if code in {
        "invalid_event",
        "invalid_batch",
        "invalid_cursor",
        "thread_not_found",
        "turn_not_found",
        "empty_transcript",
    }:
        return FailureCategory.INPUT
    return FailureCategory.INTERNAL


class AgentFailure(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(max_length=2000)
    retryable: bool = False
    category: FailureCategory = FailureCategory.INTERNAL

    @model_validator(mode="before")
    @classmethod
    def classify_missing_category(cls, value: Any) -> Any:
        if isinstance(value, dict) and "category" not in value:
            return {**value, "category": failure_category(str(value.get("code", "")))}
        return value


class KernelError(HarnessixError):
    """仅携带可公开的错误分类和消息，不持久化第三方异常原文。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(code, message, status_code=409)
        self.retryable = retryable

    def to_failure(self) -> AgentFailure:
        return AgentFailure(code=self.code, message=self.message, retryable=self.retryable)
