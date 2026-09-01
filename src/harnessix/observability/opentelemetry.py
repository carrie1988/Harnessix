from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span as OtelSpan
from opentelemetry.trace import SpanKind as OtelSpanKind
from opentelemetry.trace import Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from harnessix.domain.models import TraceContext
from harnessix.observability.core import (
    AttributeValue,
    MetricAttributes,
    ObservabilitySpan,
    SpanKind,
)

_SPAN_KINDS = {
    SpanKind.INTERNAL: OtelSpanKind.INTERNAL,
    SpanKind.SERVER: OtelSpanKind.SERVER,
    SpanKind.CONSUMER: OtelSpanKind.CONSUMER,
}


class _OpenTelemetrySpan:
    def __init__(self, span: OtelSpan) -> None:
        self._span = span

    def set_attribute(self, name: str, value: AttributeValue) -> None:
        self._span.set_attribute(name, value)


class OpenTelemetryObservability:
    """不侵入领域层的 OpenTelemetry Trace 与 Metrics 适配器。"""

    def __init__(
        self,
        *,
        service_name: str,
        endpoint: str | None = None,
        export_interval_millis: int = 10_000,
    ) -> None:
        if not service_name:
            raise ValueError("service_name 不能为空")
        if export_interval_millis <= 0:
            raise ValueError("export_interval_millis 必须大于 0")

        resource = Resource.create({"service.name": service_name})
        self._tracer_provider = TracerProvider(resource=resource)
        metric_readers = []
        if endpoint is not None:
            base_endpoint = endpoint.rstrip("/")
            self._tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base_endpoint}/v1/traces"))
            )
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{base_endpoint}/v1/metrics"),
                    export_interval_millis=export_interval_millis,
                )
            )

        self._meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
        self._tracer: Tracer = self._tracer_provider.get_tracer("harnessix")
        self._meter: Meter = self._meter_provider.get_meter("harnessix")
        self._propagator = TraceContextTextMapPropagator()
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}
        self._closed = False

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        trace_context: TraceContext | None = None,
        attributes: MetricAttributes | None = None,
    ) -> Iterator[ObservabilitySpan]:
        parent = self._extract(trace_context)
        with self._tracer.start_as_current_span(
            name,
            context=parent,
            kind=_SPAN_KINDS[kind],
            attributes=dict(attributes or {}),
        ) as span:
            yield _OpenTelemetrySpan(span)

    def current_trace_context(self) -> TraceContext | None:
        carrier: dict[str, str] = {}
        self._propagator.inject(carrier)
        traceparent = carrier.get("traceparent")
        if traceparent is None:
            return None
        return TraceContext(traceparent=traceparent, tracestate=carrier.get("tracestate"))

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        counter = self._counters.get(name)
        if counter is None:
            counter = self._meter.create_counter(name)
            self._counters[name] = counter
        counter.add(value, dict(attributes or {}))

    def record(
        self,
        name: str,
        value: float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        histogram = self._histograms.get(name)
        if histogram is None:
            histogram = self._meter.create_histogram(name, unit="s")
            self._histograms[name] = histogram
        histogram.record(value, dict(attributes or {}))

    def set_gauge(
        self,
        name: str,
        value: int | float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        gauge = self._gauges.get(name)
        if gauge is None:
            gauge = self._meter.create_gauge(name)
            self._gauges[name] = gauge
        gauge.set(value, dict(attributes or {}))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tracer_provider.shutdown()
        self._meter_provider.shutdown()

    def _extract(self, trace_context: TraceContext | None) -> Context | None:
        if trace_context is None:
            return None
        carrier = {"traceparent": trace_context.traceparent}
        if trace_context.tracestate is not None:
            carrier["tracestate"] = trace_context.tracestate
        return self._propagator.extract(carrier)
