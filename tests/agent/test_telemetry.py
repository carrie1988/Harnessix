from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from harnessix.agent.models import TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import TraceContext
from harnessix.models.contracts import ResponseFailed
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.observability.core import NoOpObservability
from harnessix.observability.opentelemetry import OpenTelemetryObservability
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_approvals import reply

CANARY = "private-canary-not-for-telemetry"


def instrumented():
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    observer = OpenTelemetryObservability(
        service_name="harnessix.kernel.test",
        span_exporter=exporter,
        metric_reader=reader,
    )
    return observer, exporter, reader


def metrics(reader):
    data = reader.get_metrics_data()
    return (
        [
            metric
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        ]
        if data
        else []
    )


async def test_durable_trace_segments_and_low_cardinality_metrics(tmp_path: Path) -> None:
    observer, exporter, reader = instrumented()
    store = SQLiteSessionStore(tmp_path / "s.db")
    tools = RecordingTools(approval=True)
    parent = TraceContext(traceparent="00-11111111111111111111111111111111-2222222222222222-01")
    try:
        async with AgentRuntime(
            store,
            ScriptedProvider([tool_step("test.read"), answer(CANARY)]),
            tools,
            observability=observer,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path / CANARY))
            turn = await runtime.run_turn(
                thread.thread_id, CANARY, request_id="r", trace_context=parent
            )
            assert turn.status == TurnStatus.WAITING_APPROVAL
        first_segments = [
            s for s in exporter.get_finished_spans() if s.name == "harnessix.agent.turn"
        ]
        assert len(first_segments) == 1 and first_segments[0].end_time is not None
        assert turn.trace_context.traceparent.split("-")[1] == parent.traceparent.split("-")[1]
        # 正常关闭不能关闭宿主注入的共享导出器。
        async with AgentRuntime(
            store,
            ScriptedProvider([tool_step("test.read"), answer(CANARY)]),
            tools,
            observability=observer,
        ) as runtime:
            await reply(runtime, thread.thread_id, turn)
            done = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            assert done.status == TurnStatus.COMPLETED
            await runtime.resume_turn(thread.thread_id, turn.turn_id)
        spans = exporter.get_finished_spans()
        assert {s.name for s in spans} == {
            "harnessix.agent.turn",
            "harnessix.agent.model",
            "harnessix.agent.tool",
            "harnessix.agent.approval",
            "harnessix.agent.recovery",
        }
        segments = [s for s in spans if s.name == "harnessix.agent.turn"]
        assert len(segments) == 2
        assert segments[0].context.trace_id == segments[1].context.trace_id
        assert segments[1].parent.span_id == segments[0].context.span_id
        assert all(s.context.trace_id == segments[0].context.trace_id for s in spans)
        assert all(s.end_time is not None for s in spans)
        exported = "\n".join(s.to_json() for s in spans)
        assert CANARY not in exported and str(tmp_path) not in exported
        metric_data = metrics(reader)
        for metric in metric_data:
            for point in metric.data.data_points:
                assert set(point.attributes) <= {"operation", "outcome", "category", "status"}
        finished = next(m for m in metric_data if m.name == "harnessix.agent.turns.finished")
        assert sum(p.value for p in finished.data.data_points) == 1
        assert "harnessix.agent.operation.duration" in {m.name for m in metric_data}
        assert CANARY not in reader.get_metrics_data().to_json()
    finally:
        observer.close()


async def test_exception_text_and_stack_never_enter_spans(tmp_path: Path) -> None:
    class FailingTools(RecordingTools):
        async def execute(self, call, cancel):
            raise RuntimeError(CANARY)

    observer, exporter, reader = instrumented()
    try:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([tool_step("test.read")]),
            FailingTools(),
            observability=observer,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, CANARY, request_id="r")
            assert turn.status == TurnStatus.FAILED
            assert CANARY not in turn.error.model_dump_json()
        spans = exporter.get_finished_spans()
        assert all(not span.events for span in spans)
        assert CANARY not in "\n".join(s.to_json() for s in spans)
        assert all(
            s.status.status_code == StatusCode.ERROR
            for s in spans
            if s.name in {"harnessix.agent.turn", "harnessix.agent.tool"}
        )
        assert CANARY not in reader.get_metrics_data().to_json()
    finally:
        observer.close()


@pytest.mark.parametrize("point", ["enter", "attribute", "exit", "counter", "record", "context"])
async def test_broken_observer_cannot_change_turn_result(
    tmp_path: Path, point: str, caplog
) -> None:
    class BrokenObserver(NoOpObservability):
        @contextmanager
        def span(self, *args, **kwargs):
            if point == "enter":
                raise RuntimeError(CANARY)

            class BrokenSpan:
                def set_attribute(self, *_):
                    if point == "attribute":
                        raise RuntimeError(CANARY)

                def set_error(self, *_):
                    pass

            yield BrokenSpan()
            if point == "exit":
                raise RuntimeError(CANARY)

        def increment(self, *args, **kwargs):
            if point == "counter":
                raise RuntimeError(CANARY)

        def record(self, *args, **kwargs):
            if point == "record":
                raise RuntimeError(CANARY)

        def current_trace_context(self):
            if point == "context":
                raise RuntimeError(CANARY)

    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        FakeProvider(),
        observability=BrokenObserver(),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        assert turn.status == TurnStatus.COMPLETED
    assert CANARY not in caplog.text


async def test_cancellation_closes_spans_and_provider_failure_retains_category(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()

    class WaitingProvider:
        async def stream(self, request, cancel):
            entered.set()
            await asyncio.Event().wait()
            yield

    observer, exporter, _ = instrumented()
    try:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            WaitingProvider(),
            observability=observer,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            task = asyncio.create_task(runtime.run_turn(thread.thread_id, "任务", request_id="r"))
            await asyncio.wait_for(entered.wait(), 2)
            turn_id = (await runtime.store.get_thread(thread.thread_id)).active_turn_id
            await runtime.cancel(thread.thread_id, turn_id)
            assert (await task).status == TurnStatus.CANCELLED
        spans = exporter.get_finished_spans()
        assert all(s.end_time for s in spans)
        assert all(
            s.attributes["outcome"] == "cancelled"
            for s in spans
            if s.name.endswith((".model", ".turn"))
        )
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "provider.db"),
            ScriptedProvider([[ResponseFailed(code="transport", retryable=True)]]),
            observability=observer,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "任务", request_id="provider")
            assert turn.error.retryable
        failed = [
            s for s in exporter.get_finished_spans() if s.status.status_code == StatusCode.ERROR
        ]
        assert failed and all(s.attributes["error.type"] == "provider" for s in failed)
    finally:
        observer.close()


async def test_export_failure_does_not_mask_original_storage_error(tmp_path: Path, caplog) -> None:
    from harnessix.agent.errors import KernelError

    class FailingStore(SQLiteSessionStore):
        async def append(self, *args, **kwargs):
            raise KernelError("storage_full", "存储空间不足")

    class FailingExit(NoOpObservability):
        @contextmanager
        def span(self, *args, **kwargs):
            with super().span(*args, **kwargs) as span:
                yield span
            raise RuntimeError(CANARY)

    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, FakeProvider()) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
    async with AgentRuntime(
        FailingStore(store.path), FakeProvider(), observability=FailingExit()
    ) as runtime:
        with pytest.raises(KernelError) as error:
            await runtime.run_turn(thread.thread_id, "任务", request_id="r")
        assert error.value.code == "storage_full"
    assert CANARY not in caplog.text
