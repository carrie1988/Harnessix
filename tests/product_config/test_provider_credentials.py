from __future__ import annotations

import httpx
import httpx2
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.contracts.provider import model_request
from tests.models.anthropic_wire import WireStream as AnthropicWireStream
from tests.models.anthropic_wire import response as anthropic_response
from tests.models.anthropic_wire import text_frames as anthropic_text_frames
from tests.models.wire import WireStream as OpenAIWireStream
from tests.models.wire import response as openai_response
from tests.models.wire import text_frames as openai_text_frames

CANARY = "managed-provider-secret-canary"


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HARNESSIX_PRODUCT_TEST_MISSING_KEY",
        "OPENAI_CUSTOM_HEADERS",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)


async def test_openai_adapter_uses_explicit_secret_without_environment_lookup() -> None:
    observed: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers.get("authorization"))
        return openai_response(OpenAIWireStream(openai_text_frames()))

    async with OpenAIChatProvider(
        OpenAIChatConfig(
            model="test-model",
            api_key_env="HARNESSIX_PRODUCT_TEST_MISSING_KEY",
            max_attempts=1,
        ),
        transport=httpx.MockTransport(handle),
        api_key=CANARY,
    ) as provider:
        events = [
            event async for event in provider.stream(model_request(with_tools=False), CancelToken())
        ]

    assert observed == [f"Bearer {CANARY}"]
    assert isinstance(events[-1], ResponseCompleted)
    assert CANARY not in "".join(event.model_dump_json() for event in events)


async def test_anthropic_adapter_uses_explicit_secret_without_environment_lookup() -> None:
    observed: list[str | None] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        observed.append(request.headers.get("x-api-key"))
        return anthropic_response(AnthropicWireStream(anthropic_text_frames()))

    async with AnthropicProvider(
        AnthropicConfig(
            model="test-model",
            api_key_env="HARNESSIX_PRODUCT_TEST_MISSING_KEY",
            max_attempts=1,
        ),
        transport=httpx2.MockTransport(handle),
        api_key=CANARY,
    ) as provider:
        events = [
            event async for event in provider.stream(model_request(with_tools=False), CancelToken())
        ]

    assert observed == [CANARY]
    assert isinstance(events[-1], ResponseCompleted)
    assert CANARY not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_explicit_secret_validation_is_bounded_and_redacted(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS" if provider == "openai" else "ANTHROPIC_CUSTOM_HEADERS",
        f"X-Canary: {CANARY}",
    )
    with pytest.raises(ValueError) as error:
        if provider == "openai":
            OpenAIChatProvider(
                OpenAIChatConfig(
                    model="test-model",
                    api_key_env="HARNESSIX_PRODUCT_TEST_MISSING_KEY",
                ),
                api_key=CANARY,
            )
        else:
            AnthropicProvider(
                AnthropicConfig(
                    model="test-model",
                    api_key_env="HARNESSIX_PRODUCT_TEST_MISSING_KEY",
                ),
                api_key=CANARY,
            )
    assert CANARY not in str(error.value)


@pytest.mark.parametrize("value", ["", "with space", "x" * 8193])
def test_explicit_secret_rejects_invalid_or_oversized_values(value: str) -> None:
    with pytest.raises(ValueError) as error:
        OpenAIChatProvider(
            OpenAIChatConfig(
                model="test-model",
                api_key_env="HARNESSIX_PRODUCT_TEST_MISSING_KEY",
            ),
            api_key=value,
        )
    assert str(error.value) == "Provider API Key未配置或格式无效"
