from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import httpx2

from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.smoke.contracts import SmokeConfig
from harnessix.smoke.runner import TEXT_MARKER
from tests.models import anthropic_wire as aw
from tests.models import wire as ow

CANARY = "fixture-SECRET-CANARY"


def config(provider="openai_chat", **updates):
    return SmokeConfig(
        provider=provider,
        base_url="https://smoke.invalid/v1",
        model="fixture-model",
        api_key_env="HARNESSIX_SMOKE_TEST_KEY",
        **updates,
    )


class WireFactory:
    def __init__(self, *, fault=None):
        self.fault = fault
        self.requests = []
        self.wires = []
        self.closed = False
        self.entered = False
        self.request_entered = asyncio.Event()
        self.sdk = None

    def _parts(self, cfg, request):
        body = json.loads(request.content)
        self.requests.append(body)
        first = len(self.requests) == 1
        tool = first and cfg.scenario != "text" and self.fault != "no_tool"
        marker = TEXT_MARKER
        if not first and not tool:
            if cfg.provider == "openai_chat":
                result = json.loads(body["messages"][-1]["content"])
            else:
                result = json.loads(body["messages"][-1]["content"][-1]["content"])
            marker = (result.get("output") or {}).get("marker", "wrong")
        if self.fault == "wrong_marker":
            marker = CANARY
        if self.fault == "repeat_tool":
            tool = True
        args = '{"unexpected":true}' if self.fault == "bad_arguments" else "{}"
        if cfg.provider == "openai_chat":
            if tool:
                parts = ow.tool_frames(args)
            else:
                parts = [
                    ow.frame(ow.chunk({"content": marker})),
                    ow.frame(ow.chunk(finish="stop")),
                    ow.frame(ow.chunk(usage=True)),
                    b"data: [DONE]\n\n",
                ]
            if self.fault == "no_usage":
                parts.pop(-2)
            if self.fault in ("truncated", "cancel", "timeout"):
                parts.pop()
        else:
            parts = (
                aw.tool_frames(args)
                if tool
                else [
                    aw.start(),
                    aw.frame(
                        "content_block_start", index=0, content_block={"type": "text", "text": ""}
                    ),
                    aw.frame(
                        "content_block_delta", index=0, delta={"type": "text_delta", "text": marker}
                    ),
                    aw.frame("content_block_stop", index=0),
                    *aw.stop(),
                ]
            )
            if self.fault == "no_usage":
                parts[0] = parts[0].replace(
                    b'"cache_read_input_tokens": 0,', b'"cache_read_input_tokens": null,'
                )
                # start 的最后字段无逗号，同样处理。
                parts[0] = parts[0].replace(
                    b'"cache_read_input_tokens": 0}', b'"cache_read_input_tokens": null}'
                )
            if self.fault in ("truncated", "cancel", "timeout"):
                parts.pop()
        return [
            (
                p.replace(b"test-model", CANARY.encode())
                .replace(b"chat-test", CANARY.encode())
                .replace(b"msg-test", CANARY.encode())
                if self.fault == "hostile_metadata"
                else p
            ).replace(tool_alias("test.read").encode(), tool_alias("smoke.read_marker").encode())
            for p in parts
        ]

    @asynccontextmanager
    async def __call__(self, cfg):
        wire_module = ow if cfg.provider == "openai_chat" else aw
        http_module = httpx if cfg.provider == "openai_chat" else httpx2

        def handle(request):
            self.request_entered.set()
            assert CANARY not in request.content.decode()
            assert (
                request.headers.get("authorization") == f"Bearer {CANARY}"
                or request.headers.get("x-api-key") == CANARY
            )
            parts = self._parts(cfg, request)
            if isinstance(self.fault, int):
                parts = [json.dumps({"error": {"message": CANARY}}).encode()]
            wire = wire_module.WireStream(parts, block=self.fault in ("cancel", "timeout"))
            self.wires.append(wire)
            if isinstance(self.fault, int):
                return http_module.Response(
                    self.fault, stream=wire, headers={"content-type": "application/json"}
                )
            return wire_module.response(wire)

        cls = OpenAIChatProvider if cfg.provider == "openai_chat" else AnthropicProvider
        async with cls(cfg.provider_config(), transport=http_module.MockTransport(handle)) as sdk:
            self.entered = True
            self.sdk = sdk
            try:
                yield sdk
            finally:
                self.closed = True
