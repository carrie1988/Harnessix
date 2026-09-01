from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harnessix.observability.opentelemetry import OpenTelemetryObservability


class _OtlpHandler(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        self.paths.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_otlp_http_exports_trace_and_metrics() -> None:
    _OtlpHandler.paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OtlpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    observability = OpenTelemetryObservability(
        service_name="harnessix.otlp-test",
        endpoint=endpoint,
        export_interval_millis=50,
    )
    try:
        with observability.span("otlp-test"):
            observability.increment("harnessix.test.counter")
            observability.set_gauge("harnessix.test.gauge", 1)
    finally:
        observability.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "/v1/traces" in _OtlpHandler.paths
    assert "/v1/metrics" in _OtlpHandler.paths
