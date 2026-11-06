"""运行报告到Langfuse的只读投影、发送与读回核验；本地证据保持权威。

对象映射（ADR 0120 第2节）：
- Dataset：计划中的数据集清单摘要；
- Experiment：计划摘要 × 组别；
- Trace：一个试次（槽位），根Span承载试次元数据；
- Observation：系统适配器提供的步骤（模型调用为generation，附用量）；
- Score：试次上的全部判分器分数，以及结果、用量、耗时。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from evalkit.contracts import Contract, Digest, EvalRunReport
from evalkit.steps import StepTrace


class LangfuseExport(Contract):
    """一次报告的完整投影；身份由报告摘要和槽位派生，重发幂等。"""

    spec_version: str = "evalkit.langfuse-export/v1"
    source_report_digest: Digest
    content_policy: str
    resource_spans: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=1)
    scores: tuple[dict[str, JsonValue], ...] = Field(max_length=200_000)


class LangfuseReadback(Contract):
    """读回核验收据：只有身份、分母、分数全部一致才写出。"""

    spec_version: str = "evalkit.langfuse-readback/v1"
    source_report_digest: Digest
    verified_trials: int = Field(ge=1)
    verified_scores: int = Field(ge=0)
    verified_steps: int = Field(ge=0)
    verified_at: datetime


def build_export(report: EvalRunReport, step_traces: tuple[StepTrace, ...]) -> LangfuseExport:
    """重校验报告后投影；元数据级别时剥离步骤正文，未知用量与费用保持未知。"""
    raise NotImplementedError("迁移自harnessix.evals.langfuse_*（ADR 0120 第4节）")


def send(export: LangfuseExport, base_url: str, public_key: str, secret_key: str) -> None:
    """OTLP/HTTP发送Trace与评分；不跟随重定向、不自动重试，部分接收即失败。"""
    raise NotImplementedError("迁移自harnessix.evals.langfuse_transport")


def read_back(
    export: LangfuseExport, base_url: str, public_key: str, secret_key: str, timeout_s: float
) -> LangfuseReadback:
    """经公开Experiments API逐项核对，支持异步可见性等待。"""
    raise NotImplementedError("迁移自harnessix.evals.langfuse_readback")
