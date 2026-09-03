"""真实 Anthropic SDK + 离线 HTTPX2 Transport；不需要真实 API 凭据。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig
from harnessix.session.sqlite import SQLiteSessionStore


class OfflineStream(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "offline",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "offline-fixture",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                    },
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "离线验收通过"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 8, "output_tokens_details": {"thinking_tokens": 1}},
            },
            {"type": "message_stop"},
        ]
        for event in events:
            yield (
                f"event: {event['type']}\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            ).encode()


def handle(request: httpx2.Request) -> httpx2.Response:
    assert request.url.host == "provider.invalid"
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, stream=OfflineStream()
    )


async def main() -> None:
    os.environ["HARNESSIX_OFFLINE_ANTHROPIC_KEY"] = "not-a-real-credential"
    config = AnthropicConfig(
        base_url="https://provider.invalid",
        model="offline-fixture",
        api_key_env="HARNESSIX_OFFLINE_ANTHROPIC_KEY",
    )
    with tempfile.TemporaryDirectory(prefix="harnessix-anthropic-") as directory:
        store = SQLiteSessionStore(Path(directory) / "session.db")
        async with AnthropicProvider(config, transport=httpx2.MockTransport(handle)) as provider:
            async with AgentRuntime(store, provider) as runtime:
                thread = await runtime.create_thread(directory)
                turn = await runtime.run_turn(
                    thread.thread_id, "验证离线链路", request_id="offline"
                )
                assert turn.status == TurnStatus.COMPLETED
                assert turn.usage.input_tokens == 15 and turn.usage.output_tokens == 8
                assert turn.usage_is_complete and len(turn.model_attempts) == 1
                attempt = turn.model_attempts[0]
                assert attempt.status == "completed" and attempt.actual_model == "offline-fixture"
                assert attempt.usage.cache_read_input_tokens == 3
                assert attempt.usage.cache_creation_input_tokens == 2
                assert attempt.usage.reasoning_output_tokens == 1
                assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                    thread.thread_id
                )
    print(
        "Anthropic SDK：真实；HTTPX2：离线替身；Turn：completed；"
        "含缓存输入：15；输出：8；尝试：1/完整；推理子集：1；Replay：一致；真实 API 调用：0"
    )


if __name__ == "__main__":
    asyncio.run(main())
