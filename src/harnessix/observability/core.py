from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from enum import StrEnum
from typing import Protocol

from harnessix.domain.models import TraceContext

AttributeValue = str | bool | int | float
MetricAttributes = Mapping[str, AttributeValue]


class SpanKind(StrEnum):
    INTERNAL = "internal"
    SERVER = "server"
    CONSUMER = "consumer"


class ObservabilitySpan(Protocol):
    def set_attribute(self, name: str, value: AttributeValue) -> None: ...


class Observability(Protocol):
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        trace_context: TraceContext | None = None,
        attributes: MetricAttributes | None = None,
    ) -> AbstractContextManager[ObservabilitySpan]: ...

    def current_trace_context(self) -> TraceContext | None: ...

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None: ...

    def record(
        self,
        name: str,
        value: float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None: ...

    def set_gauge(
        self,
        name: str,
        value: int | float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None: ...

    def close(self) -> None: ...


class _NoOpSpan:
    def set_attribute(self, name: str, value: AttributeValue) -> None:
        del name, value


_NOOP_SPAN = _NoOpSpan()


class NoOpObservability:
    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        trace_context: TraceContext | None = None,
        attributes: MetricAttributes | None = None,
    ) -> Iterator[ObservabilitySpan]:
        del name, kind, trace_context, attributes
        yield _NOOP_SPAN

    def current_trace_context(self) -> TraceContext | None:
        return None

    def increment(
        self,
        name: str,
        value: int = 1,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        del name, value, attributes

    def record(
        self,
        name: str,
        value: float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        del name, value, attributes

    def set_gauge(
        self,
        name: str,
        value: int | float,
        *,
        attributes: MetricAttributes | None = None,
    ) -> None:
        del name, value, attributes

    def close(self) -> None:
        return None
