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
