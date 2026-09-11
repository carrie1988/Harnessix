"""模型Context规划：定义Tool Result模型视图决定与检查合同。"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import ContractModel

TOOL_RESULT_VIEW_VERSION: Literal["harnessix.tool-result-model-view/v1"] = (
    "harnessix.tool-result-model-view/v1"
)
TOOL_RESULT_OMISSION_VERSION: Literal["harnessix.tool-result-omission/v1"] = (
    "harnessix.tool-result-omission/v1"
)
MODEL_HISTORY_INSPECTION_VERSION: Literal["harnessix.model-history-inspection/v1"] = (
    "harnessix.model-history-inspection/v1"
)
MODEL_HISTORY_INSPECTION_V2_VERSION: Literal["harnessix.model-history-inspection/v2"] = (
    "harnessix.model-history-inspection/v2"
)


class ToolResultViewPolicy(ContractModel):
    spec_version: Literal["harnessix.tool-result-model-view/v1"] = TOOL_RESULT_VIEW_VERSION
    max_inline_utf8_bytes: int = Field(default=64 * 1024, ge=1024, le=1_000_000, strict=True)


class ToolResultArtifactBinding(ContractModel):
    purpose: Literal["tool_result", "process_output", "batch_effect", "artifact_page"]
    artifact: ArtifactRef


class ToolResultViewDecision(ContractModel):
    spec_version: Literal["harnessix.tool-result-model-view/v1"] = TOOL_RESULT_VIEW_VERSION
    item_id: UUID
    call_id: UUID
    strategy: Literal["inline", "artifact_reference"]
    limit_utf8_bytes: int = Field(ge=1024, le=1_000_000, strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_utf8_bytes: int = Field(ge=1, le=8_388_608)
    view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_utf8_bytes: int = Field(ge=1, le=1_000_000)
    references: tuple[ToolResultArtifactBinding, ...] = Field(default_factory=tuple, max_length=2)
    replacement_output: JsonValue = None

    @model_validator(mode="after")
    def strategy_is_consistent(self) -> Self:
        purposes = [binding.purpose for binding in self.references]
        artifact_ids = [binding.artifact.artifact_id for binding in self.references]
        if len(set(purposes)) != len(purposes) or len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("Tool Result Artifact绑定重复")
        if self.view_utf8_bytes > self.limit_utf8_bytes:
            raise ValueError("Tool Result模型视图超过冻结上限")
        if self.strategy == "inline":
            if (
                self.replacement_output is not None
                or self.source_sha256 != self.view_sha256
                or self.source_utf8_bytes != self.view_utf8_bytes
            ):
                raise ValueError("Inline Tool Result不得包含替换且摘要必须一致")
            return self
        bindings = [binding for binding in self.references if binding.purpose == "tool_result"]
        if (
            self.source_utf8_bytes <= self.limit_utf8_bytes
            or len(bindings) != 1
            or not bindings[0].artifact.complete
            or not isinstance(self.replacement_output, dict)
        ):
            raise ValueError("Artifact Tool Result替换缺少完整引用或未超过上限")
        preview = self.replacement_output.get("preview")
        if (
            set(self.replacement_output) != {"preview", "artifact", "model_view"}
            or self.replacement_output.get("artifact")
            != bindings[0].artifact.model_dump(mode="json")
            or not isinstance(self.replacement_output.get("model_view"), dict)
        ):
            raise ValueError("Tool Result替换正文与冻结决定不一致")
        omission = self.replacement_output["model_view"]
        assert isinstance(omission, dict)
        field = omission.get("omitted_field")
        if (
            not isinstance(field, str)
            or field not in {"preview", "matches", "paths"}
            or (field == "preview" and preview is not None)
            or (field != "preview" and (not isinstance(preview, dict) or field in preview))
            or omission
            != {
                "spec_version": TOOL_RESULT_OMISSION_VERSION,
                "reason": "inline_budget_exceeded",
                "source_utf8_bytes": self.source_utf8_bytes,
                "source_sha256": self.source_sha256,
                "omitted_field": field,
            }
        ):
            raise ValueError("Tool Result替换正文与冻结决定不一致")
        return self


class ModelHistoryInspection(ContractModel):
    spec_version: Literal["harnessix.model-history-inspection/v1"] = (
        MODEL_HISTORY_INSPECTION_VERSION
    )
    model_step: int = Field(ge=1, le=1000)
    policy: ToolResultViewPolicy
    history_items: int = Field(ge=1, le=8192)
    tool_results: int = Field(ge=0, le=8192)
    inline_results: int = Field(ge=0, le=8192)
    artifact_reference_results: int = Field(ge=0, le=8192)
    artifact_bindings: int = Field(ge=0, le=16384)
    source_tool_result_utf8_bytes: int = Field(ge=0, le=8_388_608)
    view_tool_result_utf8_bytes: int = Field(ge=0, le=8_388_608)
    source_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def counts_are_consistent(self) -> Self:
        if self.tool_results != self.inline_results + self.artifact_reference_results:
            raise ValueError("Tool Result模型视图策略计数不一致")
        if self.tool_results > self.history_items:
            raise ValueError("Tool Result数量超过历史Item数量")
        if self.view_tool_result_utf8_bytes > self.source_tool_result_utf8_bytes:
            raise ValueError("Tool Result模型视图不得扩大正文")
        return self


class ModelHistoryInspectionV2(ContractModel):
    """绑定已发布活动窗口；原v1字段继续描述本次实际模型历史。"""

    spec_version: Literal["harnessix.model-history-inspection/v2"] = (
        MODEL_HISTORY_INSPECTION_V2_VERSION
    )
    model_step: int = Field(ge=1, le=1000)
    policy: ToolResultViewPolicy
    history_items: int = Field(ge=1, le=8192)
    tool_results: int = Field(ge=0, le=8192)
    inline_results: int = Field(ge=0, le=8192)
    artifact_reference_results: int = Field(ge=0, le=8192)
    artifact_bindings: int = Field(ge=0, le=16384)
    source_tool_result_utf8_bytes: int = Field(ge=0, le=8_388_608)
    view_tool_result_utf8_bytes: int = Field(ge=0, le=8_388_608)
    source_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_id: UUID
    window_history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_history_items: int = Field(ge=1, le=8_388_608, strict=True)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> Self:
        if self.tool_results != self.inline_results + self.artifact_reference_results:
            raise ValueError("Tool Result模型视图策略计数不一致")
        if self.tool_results > self.history_items:
            raise ValueError("Tool Result数量超过历史Item数量")
        if self.view_tool_result_utf8_bytes > self.source_tool_result_utf8_bytes:
            raise ValueError("Tool Result模型视图不得扩大正文")
        return self


ModelHistoryInspectionRecord = ModelHistoryInspection | ModelHistoryInspectionV2
