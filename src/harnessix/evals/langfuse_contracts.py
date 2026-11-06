"""Suite只读导出的版本化载体；不承载执行、凭据或新的费用结算。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue

from harnessix.domain.models import ContractModel
from harnessix.evals.harbor_contracts import HarborEvalSummary
from harnessix.evals.suite_contracts import CodingEvalSuiteSummary


class LangfuseSuiteExport(ContractModel):
    """完整Suite的脱敏实验投影；原币种成本和未知状态沿用源报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    spec_version: Literal["harnessix.langfuse-suite-export/v1"] = (
        "harnessix.langfuse-suite-export/v1"
    )
    suite_id: UUID
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: CodingEvalSuiteSummary
    resource_spans: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=1)
    scores: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=12_000)


class LangfuseHarborExport(ContractModel):
    """Harbor原生报告的脱敏投影，保持全部冻结槽位及费用未知状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    spec_version: Literal["harnessix.langfuse-harbor-export/v1"] = (
        "harnessix.langfuse-harbor-export/v1"
    )
    job_id: UUID
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: HarborEvalSummary
    resource_spans: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=1)
    scores: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=12_000)


LangfuseExport = LangfuseSuiteExport | LangfuseHarborExport


class LangfuseReadbackReport(ContractModel):
    """公开API实际读回的实验与评分核验收据；绑定源摘要且不携带服务凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    spec_version: Literal["harnessix.langfuse-readback/v1"] = "harnessix.langfuse-readback/v1"
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_trials: int = Field(ge=1, le=1000)
    verified_scores: int = Field(ge=1, le=12_000)
    verified_at: AwareDatetime
