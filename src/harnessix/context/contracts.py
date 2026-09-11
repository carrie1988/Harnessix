"""模型Context规划：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.domain.models import ContractModel

CONTEXT_INSPECTION_VERSION: Literal["harnessix.context-inspection/v1"] = (
    "harnessix.context-inspection/v1"
)
CONTEXT_INSPECTION_V2: Literal["harnessix.context-inspection/v2"] = (
    "harnessix.context-inspection/v2"
)
CONTEXT_INSPECTION_V3: Literal["harnessix.context-inspection/v3"] = (
    "harnessix.context-inspection/v3"
)
CONTEXT_SOURCE_SNAPSHOT_VERSION: Literal["harnessix.context-source-snapshot/v1"] = (
    "harnessix.context-source-snapshot/v1"
)
CONTEXT_CONSISTENCY_VERSION: Literal["harnessix.context-consistency/v1"] = (
    "harnessix.context-consistency/v1"
)
CONTEXT_ESTIMATOR: Literal["utf8-bytes/v1"] = "utf8-bytes/v1"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ContextFragmentKind(StrEnum):
    RUNTIME_INSTRUCTION = "runtime_instruction"
    USER_INSTRUCTION = "user_instruction"
    PROJECT_INSTRUCTION = "project_instruction"
    WORKSPACE = "workspace"
    GIT = "git"
    ENVIRONMENT = "environment"


class ContextTrust(StrEnum):
    RUNTIME = "runtime"
    USER = "user"
    PROJECT = "project"
    EXTERNAL = "external"


class ContextFragment(ContractModel):
    kind: ContextFragmentKind
    source: str = Field(min_length=1, max_length=4096)
    content: str = Field(min_length=1, max_length=262_144)

    @field_validator("source", "content")
    @classmethod
    def reject_nul_and_blank(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("Context Fragment 不允许空白或 NUL")
        try:
            value.encode()
        except UnicodeEncodeError:
            raise ValueError("Context Fragment 必须是有效 UTF-8 文本") from None
        return value

    @property
    def fragment_id(self) -> str:
        encoded = f"{self.kind.value}\x00{self.source}\x00{self.content}".encode()
        return hashlib.sha256(encoded).hexdigest()


class ContextSourceDocument(ContractModel):
    """动态来源的一份瞬时正文；仅其无正文快照进入 Session。"""

    source: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=262_144)
    revision: Digest

    @field_validator("source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("Context Source 文档来源不允许空白或 NUL")
        try:
            value.encode()
        except UnicodeEncodeError:
            raise ValueError("Context Source 文档来源必须是有效 UTF-8 文本") from None
        return value

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Context Source 文档正文不允许 NUL")
        try:
            value.encode()
        except UnicodeEncodeError:
            raise ValueError("Context Source 文档正文必须是有效 UTF-8 文本") from None
        return value


class ContextSourceObservation(ContractModel):
    """一次受控来源观测结果，不包含来源身份和信任级别。"""

    workspace_scope: Digest
    source_revision: Digest
    documents: tuple[ContextSourceDocument, ...] = Field(default_factory=tuple, max_length=64)

    @model_validator(mode="after")
    def unique_and_bounded(self) -> Self:
        if len({document.source for document in self.documents}) != len(self.documents):
            raise ValueError("Context Source 文档来源重复")
        if sum(len(document.content.encode()) for document in self.documents) > 131_072:
            raise ValueError("Context Source 文档正文总量超过 128 KiB")
        return self


class ContextSourceDocumentSnapshot(ContractModel):
    source: str = Field(min_length=1, max_length=4096)
    revision: Digest
    utf8_bytes: int = Field(ge=0, le=262_144)
    fragment_id: Digest | None = None

    @field_validator("source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("Context Source 快照来源不允许空白或 NUL")
        try:
            value.encode()
        except UnicodeEncodeError:
            raise ValueError("Context Source 快照来源必须是有效 UTF-8 文本") from None
        return value


class ContextSourceSnapshot(ContractModel):
    """持久化的无正文来源快照，用于解释每个模型步骤实际观察了什么。"""

    spec_version: Literal["harnessix.context-source-snapshot/v1"] = CONTEXT_SOURCE_SNAPSHOT_VERSION
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$", max_length=128)
    kind: ContextFragmentKind
    status: Literal["available", "empty"]
    workspace_scope: Digest
    source_revision: Digest
    documents: tuple[ContextSourceDocumentSnapshot, ...] = Field(
        default_factory=tuple, max_length=64
    )

    @model_validator(mode="after")
    def status_matches_documents(self) -> Self:
        fragment_ids = [document.fragment_id for document in self.documents if document.fragment_id]
        if self.status == "available" and not fragment_ids:
            raise ValueError("available Context Source 必须包含非空 Fragment")
        if self.status == "empty" and fragment_ids:
            raise ValueError("empty Context Source 不可包含 Fragment")
        if len({document.source for document in self.documents}) != len(self.documents):
            raise ValueError("Context Source 快照文档来源重复")
        if len(set(fragment_ids)) != len(fragment_ids):
            raise ValueError("Context Source 快照 Fragment 重复")
        return self


class ContextConsistencySnapshot(ContractModel):
    """多来源规划在一个有界窗口内完成稳定性复核的无正文证据。"""

    spec_version: Literal["harnessix.context-consistency/v1"] = CONTEXT_CONSISTENCY_VERSION
    strategy: Literal["optimistic-double-observation/v1"] = "optimistic-double-observation/v1"
    passes: Literal[2] = 2
    source_count: int = Field(ge=2, le=16)
    workspace_scope: Digest


class ContextLimits(ContractModel):
    context_window_tokens: int = Field(ge=1024, le=10_000_000)
    reserved_output_tokens: int = Field(ge=1, le=1_000_000)
    provider_overhead_tokens: int = Field(default=512, ge=0, le=1_000_000)
    safety_margin_tokens: int = Field(default=1024, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def leave_input_budget(self) -> Self:
        if self.available_input_tokens < 1:
            raise ValueError("Context Window 必须为输入至少保留一个 Token")
        return self

    @property
    def available_input_tokens(self) -> int:
        return (
            self.context_window_tokens
            - self.reserved_output_tokens
            - self.provider_overhead_tokens
            - self.safety_margin_tokens
        )


class ContextBuildInput(ContractModel):
    thread_id: UUID
    turn_id: UUID
    model_step: int = Field(ge=1, le=1000)
    workspace: str = Field(min_length=1, max_length=4096)
    history_documents: tuple[str, ...] = Field(default_factory=tuple, max_length=8192)
    tool_documents: tuple[str, ...] = Field(default_factory=tuple, max_length=256)

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Context Workspace 必须使用绝对路径")
        return value

    @model_validator(mode="after")
    def bound_documents(self) -> Self:
        documents = (*self.history_documents, *self.tool_documents)
        if any("\x00" in document for document in documents):
            raise ValueError("Context 文档不允许 NUL")
        try:
            size = sum(len(document.encode()) for document in documents)
        except UnicodeEncodeError:
            raise ValueError("Context 文档必须是有效 UTF-8 文本") from None
        if size > 8_388_608:
            raise ValueError("Context 文档总量超过 8 MiB")
        return self


class ContextFragmentDecision(ContractModel):
    fragment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: ContextFragmentKind
    source: str = Field(min_length=1, max_length=4096)
    trust: ContextTrust
    priority: int = Field(ge=100, le=600)
    required: bool
    estimated_tokens: int = Field(ge=1)
    disposition: Literal["included", "omitted_budget"]

    @model_validator(mode="after")
    def required_is_included(self) -> Self:
        if self.required and self.disposition != "included":
            raise ValueError("必选 Context Fragment 不可因预算省略")
        return self


class ContextInspection(ContractModel):
    spec_version: Literal["harnessix.context-inspection/v1"] = CONTEXT_INSPECTION_VERSION
    model_step: int = Field(ge=1, le=1000)
    estimator: Literal["utf8-bytes/v1"] = CONTEXT_ESTIMATOR
    limits: ContextLimits
    available_input_tokens: int = Field(ge=1)
    history_tokens: int = Field(ge=0)
    tool_tokens: int = Field(ge=0)
    instruction_tokens: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    instruction_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fragments: tuple[ContextFragmentDecision, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def internally_consistent(self) -> Self:
        if self.available_input_tokens != self.limits.available_input_tokens:
            raise ValueError("Context 可用预算与 Limits 不一致")
        expected = self.history_tokens + self.tool_tokens + self.instruction_tokens
        if self.estimated_input_tokens != expected:
            raise ValueError("Context 输入估算分项与总量不一致")
        if self.estimated_input_tokens > self.available_input_tokens:
            raise ValueError("Context 检查记录超过可用输入预算")
        if len({fragment.fragment_id for fragment in self.fragments}) != len(self.fragments):
            raise ValueError("Context Fragment 决策身份重复")
        return self


class ContextInspectionV2(ContractModel):
    spec_version: Literal["harnessix.context-inspection/v2"] = CONTEXT_INSPECTION_V2
    model_step: int = Field(ge=1, le=1000)
    estimator: Literal["utf8-bytes/v1"] = CONTEXT_ESTIMATOR
    limits: ContextLimits
    available_input_tokens: int = Field(ge=1)
    history_tokens: int = Field(ge=0)
    tool_tokens: int = Field(ge=0)
    instruction_tokens: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    instruction_fingerprint: Digest
    fragments: tuple[ContextFragmentDecision, ...] = Field(default_factory=tuple, max_length=128)
    sources: tuple[ContextSourceSnapshot, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def internally_consistent(self) -> Self:
        if self.available_input_tokens != self.limits.available_input_tokens:
            raise ValueError("Context 可用预算与 Limits 不一致")
        expected = self.history_tokens + self.tool_tokens + self.instruction_tokens
        if self.estimated_input_tokens != expected:
            raise ValueError("Context 输入估算分项与总量不一致")
        if self.estimated_input_tokens > self.available_input_tokens:
            raise ValueError("Context 检查记录超过可用输入预算")
        decisions = {fragment.fragment_id: fragment for fragment in self.fragments}
        if len(decisions) != len(self.fragments):
            raise ValueError("Context Fragment 决策身份重复")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Context Source 身份重复")
        source_fragments = [
            document.fragment_id
            for source in self.sources
            for document in source.documents
            if document.fragment_id is not None
        ]
        if len(set(source_fragments)) != len(source_fragments):
            raise ValueError("Context Fragment 不可属于多个 Source")
        if not set(source_fragments).issubset(decisions):
            raise ValueError("Context Source 快照引用了未知 Fragment")
        for source in self.sources:
            for document in source.documents:
                if document.fragment_id is None:
                    continue
                decision = decisions[document.fragment_id]
                if (
                    decision.kind != source.kind
                    or decision.source != document.source
                    or decision.estimated_tokens != max(1, document.utf8_bytes)
                ):
                    raise ValueError("Context Source 文档快照与 Fragment 决策不一致")
        return self


class ContextInspectionV3(ContractModel):
    spec_version: Literal["harnessix.context-inspection/v3"] = CONTEXT_INSPECTION_V3
    model_step: int = Field(ge=1, le=1000)
    estimator: Literal["utf8-bytes/v1"] = CONTEXT_ESTIMATOR
    limits: ContextLimits
    available_input_tokens: int = Field(ge=1)
    history_tokens: int = Field(ge=0)
    tool_tokens: int = Field(ge=0)
    instruction_tokens: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    instruction_fingerprint: Digest
    fragments: tuple[ContextFragmentDecision, ...] = Field(default_factory=tuple, max_length=128)
    sources: tuple[ContextSourceSnapshot, ...] = Field(min_length=2, max_length=16)
    consistency: ContextConsistencySnapshot

    @model_validator(mode="after")
    def internally_consistent(self) -> Self:
        if self.available_input_tokens != self.limits.available_input_tokens:
            raise ValueError("Context 可用预算与 Limits 不一致")
        expected = self.history_tokens + self.tool_tokens + self.instruction_tokens
        if self.estimated_input_tokens != expected:
            raise ValueError("Context 输入估算分项与总量不一致")
        if self.estimated_input_tokens > self.available_input_tokens:
            raise ValueError("Context 检查记录超过可用输入预算")
        decisions = {fragment.fragment_id: fragment for fragment in self.fragments}
        if len(decisions) != len(self.fragments):
            raise ValueError("Context Fragment 决策身份重复")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Context Source 身份重复")
        source_fragments = [
            document.fragment_id
            for source in self.sources
            for document in source.documents
            if document.fragment_id is not None
        ]
        if len(set(source_fragments)) != len(source_fragments):
            raise ValueError("Context Fragment 不可属于多个 Source")
        if not set(source_fragments).issubset(decisions):
            raise ValueError("Context Source 快照引用了未知 Fragment")
        for source in self.sources:
            for document in source.documents:
                if document.fragment_id is None:
                    continue
                decision = decisions[document.fragment_id]
                if (
                    decision.kind != source.kind
                    or decision.source != document.source
                    or decision.estimated_tokens != max(1, document.utf8_bytes)
                ):
                    raise ValueError("Context Source 文档快照与 Fragment 决策不一致")
        if self.consistency.source_count != len(self.sources):
            raise ValueError("Context 一致性来源数量不匹配")
        if any(
            source.workspace_scope != self.consistency.workspace_scope for source in self.sources
        ):
            raise ValueError("Context Source Workspace scope不一致")
        return self


ContextInspectionRecord = Annotated[
    ContextInspection | ContextInspectionV2 | ContextInspectionV3,
    Field(discriminator="spec_version"),
]


class PreparedContext(ContractModel):
    instructions: str | None = Field(default=None, max_length=1_000_000)
    inspection: ContextInspectionRecord

    @model_validator(mode="after")
    def fingerprint_matches(self) -> Self:
        expected = hashlib.sha256((self.instructions or "").encode()).hexdigest()
        if self.inspection.instruction_fingerprint != expected:
            raise ValueError("Context 指令与检查指纹不一致")
        if (self.instructions is None) != (self.inspection.instruction_tokens == 0):
            raise ValueError("空 Context 指令与 Token 估算不一致")
        return self


class ContextPrepared(ContractModel):
    type: Literal["context_prepared"] = "context_prepared"
    inspection: ContextInspectionRecord
