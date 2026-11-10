from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from harnessix.product_config.endpoint_probe import probe_chat_endpoint


@contextmanager
def _endpoint(models: list[str], message: dict[str, Any]) -> Iterator[tuple[str, list[Any]]]:
    seen: list[Any] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def _reply(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            seen.append(self.headers["Authorization"])
            self._reply({"data": [{"id": name} for name in models]})

        def do_POST(self) -> None:
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._reply({"choices": [{"message": message}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    finally:
        server.shutdown()


def _call(arguments: str) -> dict[str, Any]:
    return {"tool_calls": [{"function": {"name": "record_items", "arguments": arguments}}]}


def test_probe_passes_on_typed_tool_arguments_and_sends_thinking_switch() -> None:
    with _endpoint(["m"], _call('{"items": ["alpha", "beta"]}')) as (url, seen):
        results = probe_chat_endpoint(
            url, "m", "key", extra_body={"chat_template_kwargs": {"enable_thinking": False}}
        )
    assert [(item.check, item.status) for item in results] == [
        ("model", "passed"),
        ("tool_call", "passed"),
    ]
    assert seen[0] == "Bearer key"
    assert seen[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_probe_warns_on_stringified_arrays_and_fails_on_missing_calls_or_model() -> None:
    with _endpoint(["m"], _call('{"items": "[\\"alpha\\"]"}')) as (url, _):
        assert probe_chat_endpoint(url, "m", "k")[-1].status == "warning"
    with _endpoint(["m"], {"content": "I will not call tools"}) as (url, _):
        assert probe_chat_endpoint(url, "m", "k")[-1].status == "failed"
    with _endpoint(["other"], {}) as (url, _):
        results = probe_chat_endpoint(url, "m", "k")
    assert [(item.check, item.status) for item in results] == [("model", "failed")]


def test_probe_reports_an_unreachable_endpoint() -> None:
    results = probe_chat_endpoint("http://127.0.0.1:9/v1", "m", "k", timeout=2)
    assert [(item.check, item.status) for item in results] == [("endpoint", "failed")]
