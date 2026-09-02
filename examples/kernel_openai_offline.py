"""真实 SDK + 离线 HTTP Transport；不访问外部 API，不需要真实凭据。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore


class OfflineStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = [
            {
                "choices": [
                    {"index": 0, "delta": {"content": "离线 SDK 验收通过"}, "finish_reason": None}
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            },
        ]
        for chunk in chunks:
            value = {
                "id": "offline",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "offline-fixture",
                **chunk,
            }
            yield ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()
        yield b"data: [DONE]\n\n"


def handle(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "provider.invalid"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=OfflineStream()
    )


async def main() -> None:
    os.environ["HARNESSIX_OFFLINE_FIXTURE_KEY"] = "not-a-real-credential"
    config = OpenAIChatConfig(
        base_url="https://provider.invalid/v1",
        model="offline-fixture",
        api_key_env="HARNESSIX_OFFLINE_FIXTURE_KEY",
    )
    with tempfile.TemporaryDirectory(prefix="harnessix-provider-") as directory:
        store = SQLiteSessionStore(Path(directory) / "session.db")
        async with OpenAIChatProvider(config, transport=httpx.MockTransport(handle)) as provider:
            async with AgentRuntime(store, provider) as runtime:
                thread = await runtime.create_thread(directory)
                turn = await runtime.run_turn(
                    thread.thread_id, "验证离线链路", request_id="offline"
                )
                assert turn.status == TurnStatus.COMPLETED
                assert turn.usage.total_tokens == 18
                assert replay(await store.events(thread.thread_id)) == await store.get_thread(
                    thread.thread_id
                )
    print("SDK：真实；HTTP：离线替身；Turn：completed；Usage：18；Replay：一致；真实 API 调用：0")


if __name__ == "__main__":
    asyncio.run(main())
