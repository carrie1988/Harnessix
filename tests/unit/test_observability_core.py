from __future__ import annotations

import json
import logging

from harnessix.domain.models import TraceContext
from harnessix.observability import NoOpObservability, bind_log_context, build_observability
from harnessix.observability.logging import JsonLogFormatter
from harnessix.observability.opentelemetry import OpenTelemetryObservability


def test_json_log_formatter_includes_bound_safe_context() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="harnessix.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Action 已处理",
        args=(),
        exc_info=None,
    )

    with bind_log_context(
        action_id="action-a",
        tenant_id="tenant-a",
        tool="system.echo",
        password="不应出现",
    ):
        payload = json.loads(formatter.format(record))

    assert payload["message"] == "Action 已处理"
    assert payload["action_id"] == "action-a"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["tool"] == "system.echo"
    assert "password" not in payload


def test_observability_is_noop_without_endpoint() -> None:
    observability = build_observability(
        service_name="harnessix.test",
        endpoint=None,
        export_interval_millis=10_000,
    )

    assert isinstance(observability, NoOpObservability)


def test_opentelemetry_adapter_generates_w3c_trace_context() -> None:
    observability = OpenTelemetryObservability(service_name="harnessix.test")
    try:
        with observability.span("root"):
            context = observability.current_trace_context()
            assert context is not None
            assert context.traceparent.startswith("00-")
            assert len(context.traceparent) == 55

        with observability.span("continued", trace_context=context):
            continued = observability.current_trace_context()
            assert continued is not None
            assert continued.traceparent.split("-")[1] == context.traceparent.split("-")[1]
    finally:
        observability.close()


def test_invalid_parent_creates_new_valid_trace() -> None:
    observability = OpenTelemetryObservability(service_name="harnessix.test")
    try:
        with observability.span(
            "invalid-parent", trace_context=TraceContext(traceparent="invalid")
        ):
            current = observability.current_trace_context()
            assert current is not None
            assert current.traceparent != "invalid"
    finally:
        observability.close()
