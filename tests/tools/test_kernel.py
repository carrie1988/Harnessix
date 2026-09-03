from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from threading import Event

import httpx
import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ApprovalRequestContent, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models._history import tool_alias
from harnessix.models.config import OpenAIChatConfig
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools import files
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.models.wire import WireStream, chunk, frame, response, text_frames


def read_step():
    return [
        ResponseStarted(response_id="read"),
        ToolCallCompleted(call_id="read-1", tool="read_file", arguments={"path": "main.py"}),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def test_sdk_kernel_real_files_reopen_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESSIX_READ_FIXTURE_KEY", "not-a-real-credential")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('真实文件读取')\n", encoding="utf-8")
    requests, wires = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            parts = [
                frame(
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "read-wire",
                                    "type": "function",
                                    "function": {
                                        "name": tool_alias("read_file"),
                                        "arguments": '{"path":"main.py"}',
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
        else:
            assert len(requests) == 2
            messages = [m for m in body["messages"] if m["role"] == "tool"]
            assert len(messages) == 1
            assert "真实文件读取" in messages[0]["content"]
            parts = text_frames()
        wire = WireStream(parts)
        wires.append(wire)
        return response(wire)

    config = OpenAIChatConfig(
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key_env="HARNESSIX_READ_FIXTURE_KEY",
        max_attempts=1,
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root) as tools:
        async with OpenAIChatProvider(config, transport=httpx.MockTransport(handle)) as provider:
            async with AgentRuntime(store, provider, tools) as runtime:
                thread = await runtime.create_thread(str(root))
                turn = await runtime.run_turn(thread.thread_id, "读取 main.py", request_id="read")
    assert turn.status == TurnStatus.COMPLETED
    assert len(requests) == 2 and all(w.closed for w in wires)
    outputs = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert len(outputs) == 1 and outputs[0].output["text"] == "print('真实文件读取')\n"
    reopened = SQLiteSessionStore(store.path)
    await reopened.initialize()
    snapshot = await reopened.get_thread(thread.thread_id)
    assert snapshot.turns[-1] == turn
    assert replay(await reopened.events(thread.thread_id)) == snapshot


@pytest.mark.parametrize("change", ["none", "root", "policy", "workspace"])
async def test_approval_reopen_binds_workspace_and_policy(tmp_path, change):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("已批准根中的文件")
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([read_step(), answer()])
    async with CodingToolRuntime(root, require_approval=True) as tools:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "审批读取", request_id="approval")
            assert turn.status == TurnStatus.WAITING_APPROVAL
            assert not any(isinstance(i.content, ToolResultContent) for i in turn.items)
    if change == "root":
        root.rename(tmp_path / "old")
        root.mkdir()
    elif change == "workspace":
        root = tmp_path / "different"
        root.mkdir()
    options = {"denied_paths": ("private",)} if change == "policy" else {}
    next_provider = ScriptedProvider([read_step(), answer()])
    async with CodingToolRuntime(root, require_approval=True, **options) as tools:
        reopened = SQLiteSessionStore(store.path)
        async with AgentRuntime(reopened, next_provider, tools) as runtime:
            approval = next(
                i.content for i in turn.items if isinstance(i.content, ApprovalRequestContent)
            )

            async def approve():
                return await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="测试宿主"),
                )

            if change == "none":
                await approve()
                resumed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                assert resumed.status == TurnStatus.COMPLETED
                result = next(
                    i.content for i in resumed.items if isinstance(i.content, ToolResultContent)
                )
                assert result.output["text"] == "已批准根中的文件"
                assert len(next_provider.requests) == 1
            else:
                with pytest.raises(KernelError) as error:
                    await approve()
                assert error.value.code == "tool_contract_changed"
                assert not next_provider.requests
            assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
                thread.thread_id
            )


def test_input_output_schemas_match_frozen_files():
    from harnessix.tools.contracts import (
        ListFilesInput,
        ListFilesOutput,
        ReadFileInput,
        ReadFileOutput,
    )

    root = Path(__file__).parents[2] / "spec"
    frozen = {
        "list-files-input": "69769a4d0e1e5df78dc87f8545f7527d2012a00dea3713089bb36b62e996e1e8",
        "list-files-output": "2d5f4587c47af2e3fd3f3e8903ad81c73705c5a5ccd0eeb041ea390fa522e3ed",
        "read-file-input": "02de8c19904dbd60df97f5d0b57828df9d26c90433315cc7bdb179834de1b165",
        "read-file-output": "00de561ee3c379d76d4feb63d32a3014309bf22d5b3a68998536951c41eca78f",
    }
    for name, model in (
        ("list-files-input", ListFilesInput),
        ("list-files-output", ListFilesOutput),
        ("read-file-input", ReadFileInput),
        ("read-file-output", ReadFileOutput),
    ):
        assert (
            json.loads((root / f"{name}-v1.schema.json").read_text()) == model.model_json_schema()
        )
        assert (
            hashlib.sha256((root / f"{name}-v1.schema.json").read_bytes()).hexdigest()
            == frozen[name]
        )


@pytest.mark.parametrize("kind", ["user", "task"])
async def test_kernel_cancel_during_file_read_is_durable(tmp_path, monkeypatch, kind):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("未完成的读取\n")
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    decode = files._decode

    def block(data):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return decode(data)

    monkeypatch.setattr(files, "_decode", block)
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([read_step(), answer()])
    async with CodingToolRuntime(root) as tools:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(root))
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "读取中取消", request_id="cancel")
            )
            try:
                await asyncio.wait_for(entered.wait(), 10)
                turn = (await store.get_thread(thread.thread_id)).turns[-1]
                if kind == "user":
                    cancelling = await runtime.cancel(thread.thread_id, turn.turn_id)
                    assert cancelling.status == TurnStatus.CANCELLING
                else:
                    task.cancel()
                assert not task.done()
                release.set()
                if kind == "task":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    assert (await task).status == TurnStatus.CANCELLED
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            snapshot = await store.get_thread(thread.thread_id)
            assert snapshot.turns[-1].status == TurnStatus.CANCELLED
            assert len(provider.requests) == 1
            assert replay(await store.events(thread.thread_id)) == snapshot
