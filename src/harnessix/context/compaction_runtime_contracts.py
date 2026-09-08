from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.context.compaction_contracts import CompactionPolicy
from harnessix.domain.models import ContractModel


class CompactionRuntimeConfig(ContractModel):
    """显式开启轮前压缩；阈值与摘要请求上限均进入可验证配置。"""

    spec_version: Literal["harnessix.compaction-runtime/v1"] = "harnessix.compaction-runtime/v1"
    policy: CompactionPolicy
    trigger_history_tokens: int = Field(ge=513, le=8_388_608, strict=True)
    max_summary_output_tokens: int = Field(ge=1, le=65_536, strict=True)

    @model_validator(mode="after")
    def leaves_trigger_hysteresis(self) -> Self:
        if self.trigger_history_tokens <= self.policy.target_history_tokens:
            raise ValueError("自动压缩触发阈值必须高于候选窗口目标")
        return self
