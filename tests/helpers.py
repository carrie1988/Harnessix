from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from uuid import UUID

from harnessix.domain.models import TraceContext
from harnessix.observability import ObservabilitySpan, SpanKind
from harnessix.observability.core import AttributeValue, MetricAttributes
from harnessix.protocol.contracts import ThreadView
from harnessix.sdk.agent_client import AgentClient


async def wait_for_turn_status(
    client: AgentClient,
    thread_id: UUID,
    status: str,
    *,
    timeout_seconds: float = 30.0,
) -> ThreadView:
    """等待后台Turn进入目标状态，避免把关闭动作或CI调度延迟当作同步原语。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        thread = await client.get_thread(thread_id)
        turn = thread.latest_turn
        if turn is not None and turn.status == status:
            return thread
        if turn is not None and turn.status in {"completed", "failed", "cancelled", "interrupted"}:
            raise AssertionError(f"Turn提前进入非预期终态{turn.status}，期望{status}")
        if loop.time() >= deadline:
            raise AssertionError(f"Turn未在{timeout_seconds:g}秒内进入{status}")
        await asyncio.sleep(0.01)


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
