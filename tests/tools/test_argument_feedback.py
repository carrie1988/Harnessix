"""严格输入反馈只公开正式字段，并通过真实SDK链和会话回放验证修正。"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
import httpx2
import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from harnessix.agent.approvals import execution_fingerprint
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models._history import tool_alias
from harnessix.models.anthropic import AnthropicProvider
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.contracts import ReadFileInputV3
from harnessix.tools.runtime import CodingToolRuntime
from tests.models import anthropic_wire
from tests.models import wire as openai_wire
from tests.tools.test_files import call
from tests.tools.test_paging_feedback import _result_payloads


@pytest.mark.parametrize(
    "tool,arguments,required",
    [
        ("read_file", {}, "path"),
        ("list_files", {"limit": True}, "无"),
        ("glob", {"pattern": None}, "无"),
        ("grep", {}, "query"),
        ("grep", {"query": "PRIVATE_QUERY", "context_lines": 987654321}, "query"),
        ("read_artifact", {}, "artifact_id"),
    ],
)
async def test_invalid_arguments_use_registered_fields_without_io(
    tmp_path, monkeypatch, tool, arguments, required
):
    session = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(session)
    async with CodingToolRuntime(tmp_path, artifacts=artifacts) as tools:

        async def forbidden(*args, **kwargs):
            pytest.fail("输入失败不得进入工作区或Artifact读取")

        monkeypatch.setattr(tools, "_execute_read", forbidden)
        monkeypatch.setattr(tools, "_read_artifact", forbidden)
        request = call(tools, tool, **arguments)
        thread_id, turn_id = uuid4(), uuid4()
        workspace = str(tools.workspace_root)
        scope = ToolExecutionScope(
            thread_id,
            turn_id,
            request.call_id,
            workspace,
            execution_fingerprint(thread_id, turn_id, workspace, request),
        )
        result = await tools.execute_scoped(request, scope, CancelToken())
        definition = next(d for d in tools.definitions() if d.name == tool)
        fields = ", ".join(sorted(definition.input_schema["properties"]))
    assert result.outcome == "failed" and result.output is None
    assert result.error.code == "tool_invalid_arguments"
    assert result.error.category == "tool" and not result.error.retryable
    assert f"必填字段：{required}" in result.error.message
    assert f"允许字段：{fields}" in result.error.message
    assert len(result.error.message) <= 2000
    if tool == "grep" and "context_lines" in arguments:
        assert "context_lines 应满足 ≥ 0、≤ 3" in result.error.message
        assert "987654321" not in result.error.message and "PRIVATE" not in result.error.message


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": None},
        {"path": 123},
        {"path": "PRIVATE_VALUE_NEVER_IN_FEEDBACK", "max_lines": True},
        {"path": "PRIVATE_VALUE_NEVER_IN_FEEDBACK", "max_lines": 0},
        {"path": "x" * 4096},
        {"PRIVATE_FIELD_NEVER_IN_FEEDBACK": "PRIVATE_VALUE_NEVER_IN_FEEDBACK"},
        {"path": {"PRIVATE_FIELD_NEVER_IN_FEEDBACK": "PRIVATE_VALUE_NEVER_IN_FEEDBACK"}},
    ],
)
async def test_present_invalid_fields_are_not_missing_and_values_are_not_echoed(
    tmp_path, arguments
):
    async with CodingToolRuntime(tmp_path) as tools:
        result = await tools.execute(call(tools, **arguments), CancelToken())
    assert result.error.code == "tool_invalid_arguments"
    expected = "无" if "path" in arguments else "path"
    assert f"缺少必填字段：{expected}" in result.error.message
    assert "PRIVATE_FIELD_NEVER_IN_FEEDBACK" not in result.error.model_dump_json()
    assert "PRIVATE_VALUE_NEVER_IN_FEEDBACK" not in result.error.model_dump_json()
    assert "x" * 32 not in result.error.message
    assert "input_schema" in result.error.message


async def test_validator_code_does_not_expose_error_message_location_or_context(
    tmp_path, monkeypatch
):
    failure = ValidationError.from_exception_data(
        "PRIVATE_MODEL_NAME",
        [
            {
                "type": PydanticCustomError(
                    "PRIVATE_ERROR_TYPE",
                    "PRIVATE_MESSAGE {private}",
                    {"private": "PRIVATE_CONTEXT"},
                ),
                "loc": ("PRIVATE_ERROR_LOCATION",),
                "input": "PRIVATE_ERROR_INPUT",
            }
        ],
    )

    def reject(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ReadFileInputV3, "model_validate", reject)
    async with CodingToolRuntime(tmp_path) as tools:

        async def forbidden(*args, **kwargs):
            pytest.fail("校验失败不得进入I/O")

        monkeypatch.setattr(tools, "_execute_read", forbidden)
        result = await tools.execute(call(tools, path="PRIVATE_PATH"), CancelToken())
    assert result.error.code == "tool_invalid_arguments" and result.output is None
    assert "PRIVATE" not in result.error.model_dump_json()


def _tool_frames(provider_name, step, tool, arguments):
    encoded = json.dumps(arguments)
    if provider_name == "openai":
        return [
            openai_wire.frame(
                openai_wire.chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"feedback-call-{step}",
                                "type": "function",
                                "function": {"name": tool_alias(tool), "arguments": encoded},
                            }
                        ]
                    },
                    response_id=f"feedback-{step}",
                )
            ),
            openai_wire.frame(
                openai_wire.chunk(finish="tool_calls", response_id=f"feedback-{step}")
            ),
            openai_wire.frame(openai_wire.chunk(usage=True, response_id=f"feedback-{step}")),
            b"data: [DONE]\n\n",
        ]
    return [
        anthropic_wire.start(),
        anthropic_wire.frame(
            "content_block_start",
            index=0,
            content_block={
                "type": "tool_use",
                "id": f"feedback-call-{step}",
                "name": tool_alias(tool),
                "input": {},
            },
        ),
        anthropic_wire.frame(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": encoded},
        ),
        anthropic_wire.frame("content_block_stop", index=0),
        *anthropic_wire.stop("tool_use"),
    ]


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
@pytest.mark.parametrize("invalid", [{}, {"offset": 0}, {"artifact_id": None}])
async def test_sdk_artifact_feedback_new_call_actual_read_and_replay(
    tmp_path, monkeypatch, provider_name, invalid
):
    monkeypatch.setenv("HARNESSIX_FEEDBACK_FIXTURE_KEY", "fixture-key")
    monkeypatch.delenv("OPENAI_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n" * 3, encoding="utf-8")
    requests, wires = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        results = _result_payloads(provider_name, body)
        step = len(requests)
        if step == 1:
            tool, arguments = "grep", {"query": "needle", "max_results": 1}
        elif step == 2:
            assert results[-1]["outcome"] == "succeeded"
            tool, arguments = "read_artifact", invalid
        elif step == 3:
            failure = results[-1]["error"]
            assert failure["code"] == "tool_invalid_arguments"
            missing = "无" if "artifact_id" in invalid else "artifact_id"
            assert f"缺少必填字段：{missing}" in failure["message"]
            assert "允许字段：artifact_id, limit, offset" in failure["message"]
            tool, arguments = (
                "read_artifact",
                {"artifact_id": results[0]["output"]["artifact"]["artifact_id"]},
            )
        else:
            assert step == 4 and results[-1]["outcome"] == "succeeded"
            assert "needle" in results[-1]["output"]["text"]
            assert results[-1]["output"]["artifact"] == results[0]["output"]["artifact"]
            parts = (
                openai_wire.text_frames()
                if provider_name == "openai"
                else anthropic_wire.text_frames()
            )
            stream = (
                openai_wire.WireStream(parts)
                if provider_name == "openai"
                else anthropic_wire.WireStream(parts)
            )
            wires.append(stream)
            return (
                openai_wire.response(stream)
                if provider_name == "openai"
                else anthropic_wire.response(stream)
            )
        parts = _tool_frames(provider_name, step, tool, arguments)
        stream = (
            openai_wire.WireStream(parts)
            if provider_name == "openai"
            else anthropic_wire.WireStream(parts)
        )
        wires.append(stream)
        return (
            openai_wire.response(stream)
            if provider_name == "openai"
            else anthropic_wire.response(stream)
        )

    session = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(session)
    async with AsyncExitStack() as stack:
        if provider_name == "openai":
            provider = await stack.enter_async_context(
                OpenAIChatProvider(
                    OpenAIChatConfig(
                        model="test-model",
                        api_key_env="HARNESSIX_FEEDBACK_FIXTURE_KEY",
                        max_attempts=1,
                    ),
                    transport=httpx.MockTransport(handle),
                )
            )
        else:
            provider = await stack.enter_async_context(
                AnthropicProvider(
                    AnthropicConfig(
                        model="test-model",
                        api_key_env="HARNESSIX_FEEDBACK_FIXTURE_KEY",
                        max_attempts=1,
                    ),
                    transport=httpx2.MockTransport(handle),
                )
            )
        tools = await stack.enter_async_context(CodingToolRuntime(root, artifacts=artifacts))
        runtime = await stack.enter_async_context(
            AgentRuntime(session, provider, scoped_tools=tools, artifacts=artifacts)
        )
        thread = await runtime.create_thread(str(tools.workspace_root))
        turn = await runtime.run_turn(thread.thread_id, "读取完整检查Artifact", request_id="read")
    assert turn.status == TurnStatus.COMPLETED
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert [result.outcome for result in results] == ["succeeded", "failed", "succeeded"]
    assert len({result.call_id for result in results}) == 3
    assert len(requests) == 4 and all(stream.closed for stream in wires)
    reopened = SQLiteSessionStore(session.path)
    restored = await reopened.get_thread(thread.thread_id)
    assert restored.turns[-1] == turn
    assert replay(await reopened.events(thread.thread_id)) == restored


@pytest.mark.parametrize(
    ("tool", "invalid", "corrected", "hint"),
    [
        (
            "grep",
            {"query": "needle", "context_lines": 10},
            {"query": "needle", "context_lines": 3},
            "context_lines 应满足 ≥ 0、≤ 3",
        ),
        (
            "read_file",
            {"path": "example.py", "anchor": "PRIVATE_SOURCE_TEXT\nneedle"},
            {"path": "example.py", "anchor": "needle"},
            "anchor必须是不含换行或控制字符的单行字面量",
        ),
    ],
)
async def test_constraint_feedback_reaches_real_sdk_and_corrected_call_replays(
    tmp_path, monkeypatch, tool, invalid, corrected, hint
):
    monkeypatch.setenv("HARNESSIX_FEEDBACK_FIXTURE_KEY", "offline-key")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "example.py").write_text("before\nneedle\nafter\n")
    requests, wires = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        results = _result_payloads("openai", body)
        step = len(requests)
        if step == 1:
            definition = next(d for d in body["tools"] if d["function"]["name"] == tool_alias(tool))
            properties = definition["function"]["parameters"]["properties"]
            if tool == "grep":
                assert properties["context_lines"]["maximum"] == 3
            else:
                assert "单行字面量" in properties["anchor"]["description"]
            parts = _tool_frames("openai", step, tool, invalid)
        elif step == 2:
            error = results[-1]["error"]
            assert error["code"] == "tool_invalid_arguments"
            assert hint in error["message"]
            if tool == "read_file":
                assert "PRIVATE_SOURCE_TEXT" not in error["message"]
                assert "needle" not in error["message"]
            parts = _tool_frames("openai", step, tool, corrected)
        else:
            assert step == 3 and results[-1]["outcome"] == "succeeded"
            if tool == "grep":
                assert results[-1]["output"]["preview"]["matches"][0]["context"]["start_line"] == 1
            else:
                assert results[-1]["output"]["start_line"] == 2
                assert results[-1]["output"]["text"].startswith("needle\n")
            parts = openai_wire.text_frames()
        stream = openai_wire.WireStream(parts)
        wires.append(stream)
        return openai_wire.response(stream)

    session = SQLiteSessionStore(tmp_path / "session.db")
    artifacts = SQLiteArtifactStore(session)
    async with OpenAIChatProvider(
        OpenAIChatConfig(
            model="test-model", api_key_env="HARNESSIX_FEEDBACK_FIXTURE_KEY", max_attempts=1
        ),
        transport=httpx.MockTransport(handle),
    ) as provider:
        async with CodingToolRuntime(root, artifacts=artifacts) as tools:
            async with AgentRuntime(
                session, provider, scoped_tools=tools, artifacts=artifacts
            ) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                turn = await runtime.run_turn(
                    thread.thread_id, "查找needle", request_id="range-feedback"
                )
    assert turn.status is TurnStatus.COMPLETED
    results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
    assert [r.outcome for r in results] == ["failed", "succeeded"]
    assert len({r.call_id for r in results}) == 2
    assert len(requests) == 3 and all(stream.closed for stream in wires)
    reopened = SQLiteSessionStore(session.path)
    restored = await reopened.get_thread(thread.thread_id)
    assert restored.turns[-1] == turn
    assert replay(await reopened.events(thread.thread_id)) == restored
