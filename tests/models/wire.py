from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from harnessix.models._history import tool_alias


def frame(value: object) -> bytes:
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def chunk(
    delta: dict[str, Any] | None = None,
    *,
    finish: str | None = None,
    usage: bool = False,
    response_id: str = "chat-test",
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [] if usage else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if usage
        else None,
    }


def call(index: int = 0, arguments: str = "{}") -> dict[str, Any]:
    return {
        "index": index,
        "id": f"wire-call-{index}",
        "type": "function",
        "function": {"name": tool_alias("test.read"), "arguments": arguments},
    }


def text_frames() -> list[bytes]:
    return [
        frame(chunk({"role": "assistant", "content": "你"})),
        frame(chunk({"content": "好"})),
        frame(chunk(finish="stop")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]


def tool_frames(arguments: str = "{}") -> list[bytes]:
    return [
        frame(chunk({"tool_calls": [call(arguments=arguments)]})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]


class WireStream(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes], *, block: bool = False, fail: bool = False) -> None:
        self.parts = parts
        self.block = block
        self.fail = fail
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            yield part
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        if self.fail:
            raise httpx.ReadError("服务端原文不得进入诊断 SECRET-CANARY")

    async def aclose(self) -> None:
        self.closed = True


def response(stream: WireStream) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
