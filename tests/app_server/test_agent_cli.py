from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from harnessix.agent.models import TextContent, ToolResultContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent_cli import ThinAgentCLI
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.artifacts.batch_diff import SQLiteBatchDiffPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import batch_step
from tests.patches.test_managed_batches import group_case as group_case


class ScriptedConsole:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.output: list[tuple[str, str]] = []

    def write(self, text: str, *, end: str = "\n") -> None:
        self.output.append((text, end))

    async def prompt(self, text: str) -> str:
        self.output.append((text, ""))
        return self.replies.pop(0)


async def test_thin_cli_lists_all_pages_and_rejects_stalled_cursor() -> None:
    first_id = uuid4()
    second_id = uuid4()
    client = SimpleNamespace(
        list_threads=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    threads=(SimpleNamespace(thread_id=first_id, workspace="/first"),),
                    next_cursor="next",
                ),
                SimpleNamespace(
                    threads=(SimpleNamespace(thread_id=second_id, workspace="/second"),),
                    next_cursor=None,
                ),
            ]
        )
    )
    console = ScriptedConsole([])
    await ThinAgentCLI(client, console=console).list_threads()  # type: ignore[arg-type]
    assert [text for text, _ in console.output] == [f"{first_id}\t/first", f"{second_id}\t/second"]
    assert [call.kwargs["cursor"] for call in client.list_threads.await_args_list] == [None, "next"]

    stalled = SimpleNamespace(
        list_threads=AsyncMock(
            side_effect=[
                SimpleNamespace(threads=(), next_cursor="same"),
                SimpleNamespace(threads=(), next_cursor="same"),
            ]
        )
    )
    with pytest.raises(AgentSDKError) as error:
        await ThinAgentCLI(stalled).list_threads()  # type: ignore[arg-type]
    assert error.value.code == "pagination_stalled"


async def test_thin_cli_drives_question_and_preserves_model_transcript(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="ask",
                    tool="ask_user",
                    arguments={"question": "选择环境", "options": ["测试", "生产"]},
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("发布完成"),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, enable_questions=True) as runtime:
        service = AgentApplicationService(
            runtime,
            store,
            SQLiteProtocolRequestStore(store.path),
        )
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        thread = await client.create_thread(str(tmp_path), request_id="create")
        console = ScriptedConsole(["2"])
        result = await ThinAgentCLI(client, console=console).run_turn(
            thread.thread_id,
            "准备发布",
            request_id="turn",
        )
        assert result.latest_turn is not None and result.latest_turn.status == "completed"
        assert console.replies == []
        assert any(text == "2. 生产" for text, _ in console.output)
        assert any("选择环境" in text for text, _ in console.output)
        assert any("发布完成" in text for text, _ in console.output)
        history = provider.requests[1].history
        texts = [item.content.text for item in history if isinstance(item.content, TextContent)]
        assert texts == ["准备发布"]
        tool_result = next(
            item.content for item in history if isinstance(item.content, ToolResultContent)
        )
        assert tool_result.output == {"answer": "生产"}
        await client.close()


async def test_thin_cli_replays_final_text_when_turn_completed_before_follow(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([answer("快速完成")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider) as runtime:
        service = AgentApplicationService(
            runtime,
            store,
            SQLiteProtocolRequestStore(store.path),
        )
        client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
        await client.initialize()
        thread = await client.create_thread(str(tmp_path), request_id="create-fast")
        await client.start_turn(thread.thread_id, "快速任务", request_id="turn-fast")
        for _ in range(100):
            current = await client.get_thread(thread.thread_id)
            if current.latest_turn is not None and current.latest_turn.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Turn未完成")

        console = ScriptedConsole([])
        result = await ThinAgentCLI(client, console=console).follow(thread.thread_id)
        assert result.latest_turn is not None and result.latest_turn.status == "completed"
        assert [text for text, _ in console.output if text == "快速完成"] == ["快速完成"]
        await client.close()


async def test_thin_cli_reads_diff_before_batch_approval(group_case, tmp_path: Path) -> None:
    _, _, copy, _, prepared = group_case
    store = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(store)
    async with ManagedPatchBatchBridge(copy) as bridge:
        publisher = SQLiteBatchDiffPublisher(artifacts, bridge)
        provider = ScriptedProvider([batch_step(copy, bridge, prepared), answer("修改完成")])
        async with AgentRuntime(
            store,
            provider,
            patch_batches=bridge,
            batch_diffs=publisher,
        ) as runtime:
            service = AgentApplicationService(
                runtime,
                store,
                SQLiteProtocolRequestStore(store.path),
                ScopedProtocolArtifactReader(store, artifacts, publisher),
            )
            client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
            await client.initialize()
            thread = await client.create_thread(str(copy.workspace.root), request_id="create-diff")
            console = ScriptedConsole(["y"])
            result = await ThinAgentCLI(client, console=console).run_turn(
                thread.thread_id,
                "修改多个文件",
                request_id="turn-diff",
            )

            assert result.latest_turn is not None
            assert result.latest_turn.status == "completed"
            assert console.replies == []
            output = "\n".join(text for text, _ in console.output)
            assert "差异 Artifact" in output
            assert '"view": "plan"' in output
            assert output.index("差异 Artifact") < output.index("批准该操作")
            await client.close()
