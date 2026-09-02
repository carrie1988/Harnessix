from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from harnessix.models._history import tool_alias


def frame(kind: str, **fields: Any) -> bytes:
    value = {"type": kind, **fields}
    return (f"event: {kind}\ndata: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def start(**usage: Any) -> bytes:
    return frame(
        "message_start",
        message={
            "id": "msg-test",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "test-model",
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                **usage,
            },
        },
    )


def stop(reason: str = "end_turn", **usage: Any) -> list[bytes]:
    return [
        frame(
            "message_delta",
            delta={"stop_reason": reason, "stop_sequence": None},
            usage={"output_tokens": 2, **usage},
        ),
        frame("message_stop"),
    ]


def text_frames() -> list[bytes]:
    return [
        start(),
        frame("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "你"}),
        frame("content_block_delta", index=0, delta={"type": "text_delta", "text": "好"}),
        frame("content_block_stop", index=0),
        *stop(),
    ]


def tool_block(index: int = 0, arguments: str = "{}") -> list[bytes]:
    return [
        frame(
            "content_block_start",
            index=index,
            content_block={
                "type": "tool_use",
                "id": f"toolu_{index}",
                "name": tool_alias("test.read"),
                "input": {},
            },
        ),
        frame(
            "content_block_delta",
            index=index,
            delta={"type": "input_json_delta", "partial_json": arguments},
        ),
        frame("content_block_stop", index=index),
    ]


def tool_frames(arguments: str = "{}") -> list[bytes]:
    return [start(), *tool_block(arguments=arguments), *stop("tool_use")]


class WireStream(httpx2.AsyncByteStream):
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
            raise httpx2.ReadError("不得传播原始异常 SECRET-CANARY")

    async def aclose(self) -> None:
        self.closed = True


def response(wire: WireStream) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)
