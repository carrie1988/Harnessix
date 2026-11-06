"""Langfuse导出载体与读回收据；都不携带服务凭据。"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue

from evalkit.contracts import Contract, Digest


class LangfuseExport(Contract):
    """一份运行报告的完整投影；身份由报告摘要和槽位派生，重发幂等。"""

    spec_version: Literal["evalkit.langfuse-export/v1"] = "evalkit.langfuse-export/v1"
    source_report_digest: Digest
    plan_digest: Digest
    content_policy: Literal["metadata", "full"]
    resource_spans: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=1)
    scores: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=500_000)


class LangfuseReadback(Contract):
    """经公开API读回并逐项核对后的收据；只有全部槽位和分数一致才生成。"""

    spec_version: Literal["evalkit.langfuse-readback/v1"] = "evalkit.langfuse-readback/v1"
    source_report_digest: Digest
    experiments: int = Field(ge=1)
    verified_trials: int = Field(ge=1)
    verified_scores: int = Field(ge=1)
    verified_at: AwareDatetime
