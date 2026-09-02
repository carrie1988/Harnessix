from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest

from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig
from tests.contracts.provider import ProviderContract, ProviderFactory, ProviderProbe
from tests.models.anthropic_wire import (
    WireStream,
    frame,
    response,
    start,
    stop,
    text_frames,
    tool_block,
    tool_frames,
)


@pytest.fixture
def provider_factory(monkeypatch: pytest.MonkeyPatch) -> ProviderFactory:
    monkeypatch.setenv("HARNESSIX_TEST_ANTHROPIC_KEY", "fixture-key")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)

    @asynccontextmanager
    async def factory(scenario: str) -> AsyncIterator[ProviderProbe]:
        parts = text_frames()
        if scenario == "tools":
            first = tool_block(arguments='{"path":')
            first.insert(
                2,
                frame(
                    "content_block_delta",
                    index=0,
                    delta={"type": "input_json_delta", "partial_json": '"中文.txt"}'},
                ),
            )
            parts = [start(), *first, *tool_block(1), *stop("tool_use")]
        elif scenario == "bad_json":
            parts = tool_frames("{")
        elif scenario == "truncated":
            parts = tool_frames()[:-1]
        elif scenario == "duplicate_finish":
            parts = tool_frames()
            parts.insert(-1, stop("tool_use")[0])
        elif scenario == "blocked":
            parts = []
        elif scenario == "midstream":
            parts = parts[:3]
        elif scenario == "context_overflow":
            parts = [start(), *stop("model_context_window_exceeded")]
        wire = WireStream(parts, block=scenario == "blocked", fail=scenario == "midstream")
        requests: list[httpx2.Request] = []

        def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            if scenario in {"authentication", "rate_limit"}:
                return httpx2.Response(
                    401 if scenario == "authentication" else 429,
                    json={
                        "type": "error",
                        "error": {"type": scenario + "_error", "message": "SECRET-CANARY"},
                    },
                )
            return response(wire)

        async with AnthropicProvider(
            AnthropicConfig(
                model="test-model",
                api_key_env="HARNESSIX_TEST_ANTHROPIC_KEY",
                retry_delay_seconds=0,
            ),
            transport=httpx2.MockTransport(handle),
        ) as provider:
            yield ProviderProbe(provider, wire.entered, lambda: wire.closed, lambda: len(requests))

    return factory


class TestAnthropicProviderContract(ProviderContract):
    pass
