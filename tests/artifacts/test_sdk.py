from __future__ import annotations

import json

import httpx

from harnessix.agent.models import TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models._history import tool_alias
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.models.wire import WireStream, chunk, frame, response, text_frames


async def test_real_sdk_reads_beyond_preview_without_exposing_host_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESSIX_ARTIFACT_FIXTURE_KEY", "not-a-real-credential")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n" * 300)
    requests, wires = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        for internal in ("workspace_scope", "thread_id", "turn_id", "request_fingerprint", "s.db"):
            assert internal not in request.content.decode()
        outputs = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        assert all(o["outcome"] == "succeeded" for o in outputs)
        if len(requests) == 1:
            tool, args = "grep", {"query": "needle", "max_results": 2}
        elif len(requests) == 2:
            output = outputs[-1]["output"]
            assert len(output["preview"]["matches"]) == 2
            assert output["artifact"]["records"] == 300 and output["artifact"]["complete"]
            assert len(request.content) < 20000  # 完整 300 条正文没有回灌模型。
            tool, args = (
                "read_artifact",
                {"artifact_id": output["artifact"]["artifact_id"], "offset": 298},
            )
        else:
            assert len(requests) == 3
            page = outputs[-1]["output"]
            assert [json.loads(line)["line"] for line in page["text"].splitlines()] == [299, 300]
            assert page["next_offset"] is None
            wire = WireStream(text_frames())
            wires.append(wire)
            return response(wire)
        wire = WireStream(
            [
                frame(
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"artifact-{len(requests)}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_alias(tool),
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ]
                        }
                    )
                ),
                frame(chunk(finish="tool_calls")),
                frame(chunk(usage=True)),
                b"data: [DONE]\n\n",
            ]
        )
        wires.append(wire)
        return response(wire)

    config = OpenAIChatConfig(
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key_env="HARNESSIX_ARTIFACT_FIXTURE_KEY",
        max_attempts=1,
    )
    session = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(session)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with OpenAIChatProvider(config, transport=httpx.MockTransport(handle)) as provider:
            async with AgentRuntime(
                session, provider, scoped_tools=tools, artifacts=artifacts
            ) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                turn = await runtime.run_turn(thread.thread_id, "读取更多命中", request_id="sdk")
    assert turn.status == TurnStatus.COMPLETED
    assert len(requests) == 3 and all(w.closed for w in wires)
    assert replay(await session.events(thread.thread_id)) == await session.get_thread(
        thread.thread_id
    )
