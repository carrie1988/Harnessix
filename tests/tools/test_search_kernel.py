from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
from harnessix.tools import search
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GlobInput, GlobOutput, GrepInput, GrepOutput
from tests.agent.helpers import answer
from tests.models.wire import WireStream, chunk, frame, response, text_frames


def tool_step(tool):
    args = {
        "read_file": {"path": "main.py"},
        "list_files": {},
        "glob": {"pattern": "**/*.py"},
        "grep": {"query": "读取"},
    }[tool]
    return [
        ResponseStarted(response_id="read"),
        ToolCallCompleted(call_id="read-1", tool=tool, arguments=args),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


@pytest.mark.parametrize("scoped", [False, True])
async def test_sdk_glob_grep_revision_read_and_replay(tmp_path, monkeypatch, scoped):
    monkeypatch.setenv("HARNESSIX_SEARCH_FIXTURE_KEY", "not-a-real-credential")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("# 真实文件\ndef target():\n    return 42\n", encoding="utf-8")
    requests, wires = [], []

    def handle(request):
        body = json.loads(request.content)
        assert "request_fingerprint" not in request.content.decode()
        requests.append(body)
        outputs = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        assert all(o["outcome"] == "succeeded" for o in outputs)
        assert len(outputs) == len(requests) - 1
        if len(requests) == 1:
            tool, args = "glob", {"pattern": "**/*.py"}
        elif len(requests) == 2:
            assert outputs[-1]["output"]["paths"] == ["main.py"]
            tool, args = "grep", {"query": "def target", "include": "**/*.py"}
        elif len(requests) == 3:
            hit = outputs[-1]["output"]["matches"][0]
            tool, args = (
                "read_file",
                {
                    "path": hit["path"],
                    "start_line": hit["line"],
                    "expected_revision": hit["revision"],
                    "max_lines": 2,
                },
            )
        else:
            assert len(requests) == 4
            assert outputs[-1]["output"]["text"] == "def target():\n    return 42\n"
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
                                    "id": f"search-{len(requests)}",
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
        api_key_env="HARNESSIX_SEARCH_FIXTURE_KEY",
        max_attempts=1,
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root) as tools:
        async with OpenAIChatProvider(config, transport=httpx.MockTransport(handle)) as provider:
            options = {"scoped_tools": tools} if scoped else {"tools": tools}
            async with AgentRuntime(store, provider, **options) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                turn = await runtime.run_turn(
                    thread.thread_id, "定位并读取函数", request_id="search"
                )
    assert turn.status == TurnStatus.COMPLETED
    assert len(requests) == 4 and all(w.closed for w in wires)
    outputs = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert len(outputs) == 3 and all(o.outcome == "succeeded" for o in outputs)
    reopened = SQLiteSessionStore(store.path)
    assert (await reopened.get_thread(thread.thread_id)).turns[-1] == turn
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("tool", ["list_files", "read_file", "glob", "grep"])
@pytest.mark.parametrize("change", [False, True])
async def test_reopened_search_approval_and_old_contract_compatibility(
    tmp_path, monkeypatch, tool, change
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("读取夹具")
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root, require_approval=True) as tools:
        versions = {d.name: d.version for d in tools.definitions()}
        async with AgentRuntime(
            store, ScriptedProvider([tool_step(tool), answer()]), tools
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "搜索审批", request_id="approval")
            assert turn.status == TurnStatus.WAITING_APPROVAL
            assert not any(isinstance(i.content, ToolResultContent) for i in turn.items)
    if change:
        monkeypatch.setattr(search, "MAX_SEARCH_TOTAL_BYTES", search.MAX_SEARCH_TOTAL_BYTES // 2)
    provider = ScriptedProvider([tool_step(tool), answer()])
    async with CodingToolRuntime(root, require_approval=True) as tools:
        for definition in tools.definitions():
            assert (definition.version != versions[definition.name]) == (
                change and definition.name in {"glob", "grep"}
            )
        reopened = SQLiteSessionStore(store.path)
        async with AgentRuntime(reopened, provider, tools) as runtime:
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

            if change and tool in {"glob", "grep"}:
                with pytest.raises(KernelError) as error:
                    await approve()
                assert error.value.code == "tool_contract_changed" and not provider.requests
            else:
                await approve()
                resumed = await runtime.resume_turn(thread.thread_id, turn.turn_id)
                assert resumed.status == TurnStatus.COMPLETED
                results = [
                    i.content for i in resumed.items if isinstance(i.content, ToolResultContent)
                ]
                assert len(results) == 1 and results[0].outcome == "succeeded"
                assert len(provider.requests) == 1
            assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
                thread.thread_id
            )


@pytest.mark.parametrize(
    "name,model,frozen",
    [
        (
            "glob-input",
            GlobInput,
            "5b7e407d90782eedd5967bd77e1dc5a52342a07aa21f9914cba74651a187689c",
        ),
        (
            "glob-output",
            GlobOutput,
            "741151585d31cb5834e333c4d5ff94b326ebe30a3cb10785be7c24f2dcf1b1a1",
        ),
        (
            "grep-input",
            GrepInput,
            "a801eed4c6755949524bb5044b59df1b2456cd0f1f88df5fbc7aca59c79b6e14",
        ),
        (
            "grep-output",
            GrepOutput,
            "8b9aa9306bfdd4ddb2f517277fb6c245a705873c3999c9190852866abb8aa83e",
        ),
    ],
)
def test_frozen_search_schema(name, model, frozen):
    path = Path(__file__).parents[2] / "spec" / f"{name}-v1.schema.json"
    assert json.loads(path.read_text()) == model.model_json_schema()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen
