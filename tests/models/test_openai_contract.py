from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from harnessix.models.config import OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.contracts.provider import ProviderContract, ProviderFactory, ProviderProbe
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames, tool_frames


@pytest.fixture
def provider_factory(monkeypatch: pytest.MonkeyPatch) -> ProviderFactory:
    monkeypatch.setenv("HARNESSIX_TEST_KEY", "fixture-key")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)

    @asynccontextmanager
    async def factory(scenario: str) -> AsyncIterator[ProviderProbe]:
        parts = text_frames()
        if scenario == "tools":
            parts = [
                frame(chunk({"tool_calls": [call(0, '{"path":'), call(1, "{")]})),
                frame(
                    chunk(
                        {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": "}"}},
                                {"index": 0, "function": {"arguments": '"中文.txt"}'}},
                            ]
                        }
                    )
                ),
                *tool_frames()[1:],
            ]
        elif scenario == "bad_json":
            parts = tool_frames("{")
        elif scenario == "truncated":
            parts = tool_frames()[:-1]
        elif scenario == "duplicate_finish":
            parts = tool_frames()
            parts.insert(2, frame(chunk(finish="tool_calls")))
        elif scenario == "blocked":
            parts = []
        elif scenario == "midstream":
            parts = parts[:1]
        wire = WireStream(parts, block=scenario == "blocked", fail=scenario == "midstream")
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            statuses = {"authentication": 401, "context_overflow": 400, "rate_limit": 429}
            if scenario in statuses:
                code = "context_length_exceeded" if scenario == "context_overflow" else scenario
                return httpx.Response(
                    statuses[scenario],
                    json={"error": {"code": code, "message": "SECRET-CANARY"}},
                )
            return response(wire)

        async with OpenAIChatProvider(
            OpenAIChatConfig(
                model="test-model", api_key_env="HARNESSIX_TEST_KEY", retry_delay_seconds=0
            ),
            transport=httpx.MockTransport(handle),
        ) as provider:
            yield ProviderProbe(provider, wire.entered, lambda: wire.closed, lambda: len(requests))

    return factory


class TestOpenAIProviderContract(ProviderContract):
    pass
