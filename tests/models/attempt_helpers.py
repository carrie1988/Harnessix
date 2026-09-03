from dataclasses import dataclass

import httpx
import httpx2

from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from tests.models import anthropic_wire
from tests.models import wire as openai_wire

KEY_ENV = "HARNESSIX_LEDGER_TEST_KEY"
CANARY = "ledger-test-credential-not-for-events"


@dataclass
class Adapter:
    kind: str

    @property
    def wire(self):
        return openai_wire if self.kind == "openai" else anthropic_wire

    @property
    def http(self):
        return httpx if self.kind == "openai" else httpx2

    def provider(self, wire, *, handler=None, **options):
        config_type, provider_type = (
            (OpenAIChatConfig, OpenAIChatProvider)
            if self.kind == "openai"
            else (AnthropicConfig, AnthropicProvider)
        )
        return provider_type(
            config_type(
                model="requested-model", api_key_env=KEY_ENV, retry_delay_seconds=0, **options
            ),
            transport=self.http.MockTransport(handler or (lambda _: self.wire.response(wire))),
        )

    def error(self, status=503):
        return self.http.Response(
            status,
            json={"error": {"code": "server_error", "type": "overloaded_error", "message": CANARY}},
        )

    def detailed_frames(self):
        parts = self.wire.text_frames()
        if self.kind == "openai":
            value = openai_wire.chunk(usage=True)
            value["usage"].update(
                prompt_tokens_details={"cached_tokens": 4, "cache_write_tokens": 3},
                completion_tokens_details={"reasoning_tokens": 1},
            )
            parts[-2] = openai_wire.frame(value)
        else:
            parts[0] = anthropic_wire.start(
                input_tokens=3, cache_read_input_tokens=4, cache_creation_input_tokens=3
            )
            parts[-2:] = anthropic_wire.stop(output_tokens_details={"thinking_tokens": 1})
        return parts
