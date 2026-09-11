"""可观测性：汇总并导出受支持的公共入口，不承载运行时编排。"""

from __future__ import annotations

from harnessix.observability.core import (
    NoOpObservability,
    Observability,
    ObservabilitySpan,
    SpanKind,
)
from harnessix.observability.logging import bind_log_context, configure_logging, trace_log_fields


def build_observability(
    *,
    service_name: str,
    endpoint: str | None,
    export_interval_millis: int,
) -> Observability:
    """根据配置构造No-op或OpenTelemetry实现。"""
    if endpoint is None:
        return NoOpObservability()
    try:
        from harnessix.observability.opentelemetry import OpenTelemetryObservability
    except ImportError as error:
        raise RuntimeError("启用 OpenTelemetry 需要安装 harnessix[observability]") from error
    return OpenTelemetryObservability(
        service_name=service_name,
        endpoint=endpoint,
        export_interval_millis=export_interval_millis,
    )


__all__ = [
    "NoOpObservability",
    "Observability",
    "ObservabilitySpan",
    "SpanKind",
    "bind_log_context",
    "build_observability",
    "configure_logging",
    "trace_log_fields",
]
