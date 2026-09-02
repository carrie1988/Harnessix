"""离线验收有限时长 Span、审批重启关联与低基数指标，不连接 Collector。"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from examples.kernel_approval import FixtureReader, provider
from harnessix.agent.models import ApprovalRequestContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.observability.opentelemetry import OpenTelemetryObservability
from harnessix.session.sqlite import SQLiteSessionStore


async def main() -> None:
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    observer = OpenTelemetryObservability(
        service_name="harnessix.kernel.acceptance",
        span_exporter=exporter,
        metric_reader=reader,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="harnessix-telemetry-") as directory:
            store = SQLiteSessionStore(Path(directory) / "session.db")
            tools = FixtureReader()
            async with AgentRuntime(store, provider(), tools, observability=observer) as runtime:
                thread = await runtime.create_thread(directory)
                turn = await runtime.run_turn(
                    thread.thread_id, "审批并读取固定数据", request_id="r"
                )
                assert turn.status == TurnStatus.WAITING_APPROVAL
            async with AgentRuntime(store, provider(), tools, observability=observer) as runtime:
                approval = next(
                    i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
                )
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="离线验收"),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            spans = exporter.get_finished_spans()
            assert all(s.end_time is not None for s in spans)
            assert len({s.context.trace_id for s in spans}) == 1
            data = reader.get_metrics_data()
            assert data is not None
            metrics = [
                m
                for resource in data.resource_metrics
                for scope in resource.scope_metrics
                for m in scope.metrics
            ]
            assert all(
                set(point.attributes) <= {"operation", "outcome", "category", "status"}
                for m in metrics
                for point in m.data.data_points
            )
            print(
                json.dumps(
                    {
                        "turn_status": turn.status.value,
                        "tool_executions": tools.calls,
                        "closed_spans": len(spans),
                        "trace_count": len({s.context.trace_id for s in spans}),
                        "metrics": sorted(m.name for m in metrics),
                        "external_model_requests": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    finally:
        observer.close()


if __name__ == "__main__":
    asyncio.run(main())
