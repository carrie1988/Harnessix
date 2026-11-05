"""A scripted OpenAI-compatible chat model for end-to-end demos and acceptance runs (no LLM).

Each request is answered by the step numbered by the assistant messages already present: a tool
call from the script (tool names are mapped to the wire aliases advertised in the request), then a
final message once the script is exhausted. Responses stream like the Chat Completions API.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Step = tuple[str, dict[str, Any]]


def start_scripted_model(
    steps: list[Step], final: str = "Done."
) -> tuple[ThreadingHTTPServer, str]:
    """Serve `steps` on 127.0.0.1; returns (server, base_url). Call server.shutdown() to stop."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            step = sum(1 for message in body["messages"] if message.get("role") == "assistant")
            aliases = {
                tool["function"]["description"].split(": ", 1)[0]: tool["function"]["name"]
                for tool in body.get("tools", [])
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk: dict[str, Any] = {
                "id": f"c{step}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "scripted",
            }
            delta: dict[str, Any]
            if step < len(steps) and steps[step][0] in aliases:
                tool, arguments = steps[step]
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{step}",
                            "type": "function",
                            "function": {"name": aliases[tool], "arguments": json.dumps(arguments)},
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": final}, "stop"
            usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
            for payload in (
                {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                {**chunk, "choices": [], "usage": usage},
            ):
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"
