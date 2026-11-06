"""离线验收用OpenAI SSE夹具；只根据真实工具反馈作答，不模拟产品运行时。"""

from __future__ import annotations

import json
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    """响应受控验收指令，禁止记录请求、Header或响应正文。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 2 * 1024 * 1024:
            self.send_error(413)
            return
        request = json.loads(self.rfile.read(size))
        user = " ".join(
            message["content"] for message in request["messages"] if message["role"] == "user"
        )
        match = re.search(r"mode=([a-z-]+)", user)
        mode = match.group(1) if match else "pass"
        if mode == "provider-fail":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "deadline":
            threading.Event().wait(120)
            return
        tools = [message for message in request["messages"] if message["role"] == "tool"]
        if mode == "approval":
            delta = self._call(
                "apply_patch_batch",
                {
                    "files": [
                        {
                            "operation": "create",
                            "path": "unapproved.txt",
                            "content": "must-not-be-created\n",
                            "mode": 420,
                        }
                    ]
                },
            )
            finish = "tool_calls"
        elif not tools:
            delta = self._call("read_file", {"path": "exercise.txt"})
            finish = "tool_calls"
        else:
            feedback = json.loads(tools[-1]["content"])
            text = (feedback.get("output") or {}).get("text", "")
            found = re.search(r"value=(\d+)", text)
            answer = (
                f"REVIEW:{found.group(1)}"
                if feedback.get("outcome") == "succeeded" and found
                else "TOOL_FEEDBACK_MISSING"
            )
            delta = {
                "role": "assistant",
                "content": "WRONG_REVIEW" if mode == "task-fail" else answer,
            }
            finish = "stop"
        self._stream(request["model"], delta, finish)

    @staticmethod
    def _call(name, arguments):
        return {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "fixture-call",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        }

    def _stream(self, model, delta, finish):
        base = {
            "id": "fixture-response",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
        }
        chunks = [
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {
                **base,
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        ]
        body = (
            b"".join(("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks)
            + b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8411), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/opt/harnessix/fixture.crt", "/opt/harnessix/fixture.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
