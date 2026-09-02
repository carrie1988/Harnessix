from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from harnessix.domain.models import (
    ActionContext,
    ActionRequest,
    EffectClass,
    Principal,
    TraceContext,
)
from harnessix.observability import ObservabilitySpan, SpanKind
from harnessix.observability.core import AttributeValue, MetricAttributes


class RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, AttributeValue] = {}

    def set_attribute(self, name: str, value: AttributeValue) -> None:
        self.attributes[name] = value

    def set_error(self, category: str) -> None:
        self.attributes["error.type"] = category


class RecordingObservability:
    def __init__(self, trace_context: TraceContext | None = None) -> None:
        self.trace_context = trace_context
        self.spans: list[tuple[str, SpanKind, TraceContext | None]] = []
        self.metrics: list[tuple[str, str, float, dict[str, AttributeValue]]] = []
        self.closed = False

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        trace_context: TraceContext | None = None,
        attributes: MetricAttributes | None = None,
    ) -> Iterator[ObservabilitySpan]:
        del attributes
        self.spans.append((name, kind, trace_context))
        yield RecordingSpan()

    def current_trace_context(self) -> TraceContext | None:
        return self.trace_context

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        self._metric("counter", name, value, attributes)

    def record(
        self,
        name: str,
        value: float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        self._metric("histogram", name, value, attributes)

    def set_gauge(
        self,
        name: str,
        value: int | float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        self._metric("gauge", name, value, attributes)

    def close(self) -> None:
        self.closed = True

    def _metric(
        self,
        kind: str,
        name: str,
        value: int | float,
        attributes: Mapping[str, AttributeValue] | None,
    ) -> None:
        self.metrics.append((kind, name, float(value), dict(attributes or {})))


def action_request(
    tool: str,
    arguments: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    effect_hint: EffectClass | None = None,
    metadata: dict[str, Any] | None = None,
) -> ActionRequest:
    return ActionRequest(
        tool=tool,
        arguments=arguments,
        principal=Principal(tenant_id="tenant-a", subject_id="agent-a", framework="test-agent"),
        context=ActionContext(session_id="session-a", run_id="run-a"),
        idempotency_key=idempotency_key,
        effect_hint=effect_hint,
        metadata=metadata or {},
    )
