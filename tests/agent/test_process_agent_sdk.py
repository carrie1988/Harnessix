from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest

from harnessix.agent.models import ProcessApprovalRequestContent, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ActionStatus, ApprovalDecision, ApprovalOutcome, Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ModelProvider
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.worker import ActionWorker
from tests.models import anthropic_wire as aw
from tests.models import wire as ow


def _approval(turn) -> ProcessApprovalRequestContent:
    return next(
        item.content
        for item in turn.items
        if isinstance(item.content, ProcessApprovalRequestContent)
    )


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
async def test_real_sdk_process_approval_worker_artifact_and_private_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vendor: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    marker = tmp_path / "execution-count"
    session_path = tmp_path / "session.db"
    effects_path = tmp_path / "effects.db"
    monkeypatch.setenv("HARNESSIX_PROCESS_FIXTURE_KEY", "offline-fixture-not-a-credential")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    requests: list[dict[str, object]] = []
    streams: list[ow.WireStream | aw.WireStream] = []

    def outputs(body: dict[str, object]) -> list[dict[str, object]]:
        messages = body["messages"]
        assert isinstance(messages, list)
        if vendor == "openai":
            return [
                json.loads(message["content"]) for message in messages if message["role"] == "tool"
            ]
        return [
            json.loads(content["content"])
            for message in messages
            if isinstance(message["content"], list)
            for content in message["content"]
            if content["type"] == "tool_result"
        ]

    code = (
        "from pathlib import Path; import os,sys; p=Path(sys.argv[1]); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0)+1)); "
        "os.write(1,b'private-output-\\x00\\xff\\n')"
    )

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        previous = outputs(body)
        tools = body["tools"]
        names = {tool["function"]["name"] if vendor == "openai" else tool["name"] for tool in tools}
        assert {tool_alias("host.process"), tool_alias("read_artifact")} <= names
        if index == 1:
            assert previous == []
            name = "host.process"
            arguments = {
                "program": "python",
                "arguments": ["-I", "-c", code, str(marker)],
                "timeout_seconds": 5.0,
            }
        elif index == 2:
            result = previous[-1]
            assert result["outcome"] == "succeeded"
            output = result["output"]
            assert output["action_status"] == "succeeded"
            assert output["stdout"]["captured_bytes"] == len(b"private-output-\x00\xff\n")
            assert "data_base64" not in json.dumps(result)
            reference = output["artifact"]
            assert reference["complete"] is True
            name = "read_artifact"
            arguments = {
                "artifact_id": reference["artifact_id"],
                "offset": 0,
                "limit": 1,
            }
        else:
            assert index == 3
            page = previous[-1]["output"]
            summary = json.loads(page["text"])
            assert summary["kind"] == "summary" and summary["complete"] is True
            assert summary["stdout"]["captured_bytes"] == len(b"private-output-\x00\xff\n")
            assert page["next_offset"] == 1
            name = None
            arguments = None

        wire = ow if vendor == "openai" else aw
        if name is None:
            frames = wire.text_frames()
        elif vendor == "openai":
            frames = [
                ow.frame(
                    ow.chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"process-sdk-{index}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_alias(name),
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ]
                        }
                    )
                ),
                ow.frame(ow.chunk(finish="tool_calls")),
                ow.frame(ow.chunk(usage=True)),
                b"data: [DONE]\n\n",
            ]
        else:
            frames = [
                aw.start(),
                aw.frame(
                    "content_block_start",
                    index=0,
                    content_block={
                        "type": "tool_use",
                        "id": f"process-sdk-{index}",
                        "name": tool_alias(name),
                        "input": {},
                    },
                ),
                aw.frame(
                    "content_block_delta",
                    index=0,
                    delta={
                        "type": "input_json_delta",
                        "partial_json": json.dumps(arguments),
                    },
                ),
                aw.frame("content_block_stop", index=0),
                *aw.stop("tool_use"),
            ]
        stream = wire.WireStream(frames)
        streams.append(stream)
        return wire.response(stream)

    def provider():
        if vendor == "openai":
            return OpenAIChatProvider(
                OpenAIChatConfig(
                    model="test-model",
                    base_url="https://provider.invalid/v1",
                    api_key_env="HARNESSIX_PROCESS_FIXTURE_KEY",
                    max_attempts=1,
                ),
                transport=httpx.MockTransport(handle),
            )
        return AnthropicProvider(
            AnthropicConfig(
                model="test-model",
                base_url="https://provider.invalid",
                api_key_env="HARNESSIX_PROCESS_FIXTURE_KEY",
                max_attempts=1,
            ),
            transport=httpx2.MockTransport(handle),
        )

    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(effects_path),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    bridge = ProcessAgentBridge(
        actions,
        Principal(
            tenant_id="tenant-a",
            subject_id="agent-a",
            framework="harnessix-agent",
        ),
    )
    session = SQLiteSessionStore(session_path)
    artifacts = SQLiteArtifactStore(session)

    @asynccontextmanager
    async def process_runtime(model: ModelProvider) -> AsyncIterator[AgentRuntime]:
        async with CodingToolRuntime(root, artifacts=artifacts) as tools:
            publisher = SQLiteProcessArtifactPublisher(
                artifacts,
                bridge,
                workspace_scope=tools.workspace_scope,
            )
            async with AgentRuntime(
                session,
                model,
                scoped_tools=tools,
                artifacts=artifacts,
                processes=bridge,
                process_artifacts=publisher,
            ) as runtime:
                yield runtime

    try:
        async with provider() as model:
            async with process_runtime(model) as runtime:
                thread = await runtime.create_thread(str(root))
                pending = await runtime.run_turn(
                    thread.thread_id,
                    "执行进程并读取归档摘要",
                    request_id="process-sdk",
                )
                assert pending.status is TurnStatus.WAITING_APPROVAL and len(requests) == 1
                request = _approval(pending)

        async with provider() as model:
            async with process_runtime(model) as runtime:
                waiting = await runtime.reply_approval(
                    thread.thread_id,
                    pending.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED,
                        actor="sdk-reviewer",
                    ),
                )
                assert waiting.status is TurnStatus.WAITING_ACTION and len(requests) == 1

        completed_action = await ActionWorker(
            actions,
            poll_seconds=0.01,
            heartbeat_seconds=1,
            recovery_interval_seconds=1,
        ).run_once()
        assert completed_action is not None and completed_action.status is ActionStatus.SUCCEEDED

        async with provider() as model:
            async with process_runtime(model) as runtime:
                completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
        assert completed.status is TurnStatus.COMPLETED
    finally:
        await actions.close()

    assert marker.read_text() == "1"
    assert len(requests) == 3 and all(stream.closed for stream in streams)
    process_results = [
        item.content
        for item in completed.items
        if isinstance(item.content, ToolResultContent) and item.content.process is not None
    ]
    assert len(process_results) == 1 and process_results[0].outcome == "succeeded"
    with sqlite3.connect(effects_path) as database:
        assert database.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
    with sqlite3.connect(session_path) as database:
        assert database.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
    public = json.dumps(requests)
    for private in (
        str(request.plan.action_id),
        request.plan.action_fingerprint,
        request.plan.binding_fingerprint,
        request.plan.approval_fingerprint,
        request.plan.idempotency_key,
        request.plan.request_id,
        '"action_id":',
        '"process":',
        "data_base64",
    ):
        assert private not in public
    assert replay(await session.events(thread.thread_id)) == await session.get_thread(
        thread.thread_id
    )
