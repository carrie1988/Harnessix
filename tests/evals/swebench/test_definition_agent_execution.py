"""Mock模型经正式SWE装配实际读取定义；不执行模型API、容器或真实评测。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.evals.swebench import agent_execution, execution
from harnessix.evals.swebench.run_config import SweBenchDefinitionAgentRunConfig
from harnessix.models._tool_schema import provider_tool_schema
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.product_config import action_runtime
from harnessix.product_config.process_profile import probe_product_process_profile
from harnessix.tools.contracts import ReadFileInputV3, ReadFileInputV4
from tests.evals.swebench.test_agent_execution import _process_profile
from tests.evals.swebench.test_execution import _config, _plan, _turn
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames
from tests.product_config.test_process_action import _probe_runner


@pytest.mark.parametrize("enabled", [False, True])
async def test_frozen_definition_policy_reaches_case_entrypoint(tmp_path, monkeypatch, enabled):
    plan = _plan()
    original = _config(tmp_path, plan)
    config = (
        SweBenchDefinitionAgentRunConfig.model_validate(
            {**original.model_dump(), "spec_version": "harnessix.swebench-agent-run-config/v12"}
        )
        if enabled
        else original
    )
    calls = []

    async def run_case(**arguments):
        calls.append(arguments)
        turn = _turn(0)
        return turn.turn_id, turn

    monkeypatch.setattr(
        execution, "read_case_execution_state", lambda *_: SimpleNamespace(status="ready")
    )
    monkeypatch.setattr(execution, "run_swebench_agent_case", run_case)
    await execution._open_case_turn(
        plan,
        config,
        plan.cases[0],
        tmp_path / "case",
        tmp_path / "workspace",
        config.process_profile_for(plan.cases[0].instance_id),
        "offline",
        CancelToken(),
    )
    assert len(calls) == 1
    assert calls[0]["enable_definition_views"] is enabled
    assert calls[0]["budget_pressure_compaction"] is enabled
    assert calls[0]["execution_fingerprint"] == config.fingerprint
    for flag in (
        "enable_regex_search",
        "reserve_closing_phase",
        "enable_grep_snapshots",
        "allow_optional_pre_edit_profile",
        "enable_planning",
    ):
        assert calls[0][flag] is False


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_agent_publishes_opt_in_definition_header_body_raw_sha_and_context(
    tmp_path, monkeypatch, enabled
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "run").mkdir(mode=0o700)
    # 多行头、Unicode文档和CRLF可区分真实原文、连续正文与归一化摘要。
    source = (
        "# unrelated module context\r\n"
        "def choose(\r\n"
        "    value,\r\n"
        "):\r\n"
        '    """中文 documentation.\r\n'
        "    More detail, not implementation.\r\n"
        '    """\r\n'
        "    result = value + 1\r\n"
        "    return result\r\n"
        "def unrelated():\r\n"
        "    return 0\r\n"
    ).encode()
    path = workspace / "source.py"
    path.write_bytes(source)
    plan = _plan()
    requests = []
    arguments = {"path": "source.py", "anchor": "def choose(", "max_lines": 60}
    if enabled:
        arguments["view"] = "definition"
    read_call = call(0, json.dumps(arguments))
    read_call["function"]["name"] = "read_file"
    frames = [
        frame(chunk({"tool_calls": [read_call]})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return response(WireStream(frames if len(requests) == 1 else text_frames()))

    monkeypatch.setenv("DEFINITION_VIEW_OFFLINE_KEY", "offline-placeholder")
    config = OpenAIChatConfig(
        model="test-model",
        api_key_env="DEFINITION_VIEW_OFFLINE_KEY",
        max_attempts=1,
        capabilities=ChatCapabilities(tool_calls=True),
    )
    # 只替代容器探测和模型网络；Session、Context、Catalog、read_file均走正式实现。
    monkeypatch.setattr(
        action_runtime,
        "probe_product_process_profile",
        lambda profile, supervisor, secrets: probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        ),
    )
    monkeypatch.setattr(
        agent_execution,
        "_provider",
        lambda provider_config, _key: OpenAIChatProvider(
            provider_config, transport=httpx.MockTransport(handler)
        ),
    )
    thread_id, turn = await agent_execution.run_swebench_agent_case(
        plan=plan,
        case=plan.cases[0],
        run_root=tmp_path / "run",
        workspace=workspace,
        git_executable=Path("/usr/bin/git"),
        provider_config=config,
        execution_fingerprint="a" * 64,
        process_profile=_process_profile(tmp_path),
        api_key="offline-placeholder",
        budget_pressure_compaction=enabled,
        **({"enable_definition_views": True} if enabled else {}),
    )
    assert turn.status == TurnStatus.COMPLETED and thread_id is not None
    assert len(requests) == turn.model_steps == 2
    read_file = next(
        tool["function"] for tool in requests[0]["tools"] if tool["function"]["name"] == "read_file"
    )
    input_model = ReadFileInputV4 if enabled else ReadFileInputV3
    assert read_file["parameters"] == provider_tool_schema(input_model.model_json_schema())
    tool_calls = [item.content for item in turn.items if isinstance(item.content, ToolCallContent)]
    assert len(tool_calls) == 1 and tool_calls[0].tool == "read_file"
    assert tool_calls[0].tool_version.startswith("3." if enabled else "2.")
    assert tool_calls[0].arguments == arguments
    messages = json.dumps(requests[0]["messages"])
    assert ("harnessix.coding-instructions/v25" in messages) is enabled
    assert ("harnessix.definition-view-guidance/v1" in messages) is enabled
    assert "grep is not regex" in messages
    result = next(message for message in requests[1]["messages"] if message["role"] == "tool")
    payload = json.loads(result["content"])
    assert payload["outcome"] == "succeeded"
    output = payload["output"]
    assert output["content_sha256"] == hashlib.sha256(source).hexdigest()
    assert output["digest_status"] == "complete" and output["file_bytes"] == len(source)
    assert output["revision"] != output["content_sha256"]
    lines = source.decode("utf-8").splitlines(keepends=True)
    if enabled:
        definition = output["definition"]
        assert definition["spec_version"] == "harnessix.definition-view/v1"
        assert definition["language"] == "python" and definition["kind"] == "function"
        assert definition["header"] == {"start_line": 2, "end_line": 4, "text": "".join(lines[1:4])}
        assert definition["omitted_docstring"] == {"start_line": 5, "end_line": 7}
        assert definition["body_start_line"] == output["start_line"] == 8
        assert definition["body_end_line"] == output["end_line"] == 9
        assert output["text"] == "".join(lines[7:9])
        assert not output["truncated"] and output["next_line"] is None
    else:
        assert "harnessix.coding-instructions/v22" in messages
        assert "definition" not in output
        assert output["start_line"] == 2 and output["end_line"] == 11
        assert output["text"] == "".join(lines[1:])
    assert path.read_bytes() == source
    assert turn.budget == agent_execution._turn_budget(plan)
    assert (tmp_path / "run" / "session.sqlite").is_file()


@pytest.mark.parametrize("invalid", [None, 1, "true"])
async def test_invalid_definition_flag_fails_before_provider_or_publication(
    tmp_path, monkeypatch, invalid
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法定义视图开关不得开启Provider或发布状态")

    monkeypatch.setattr(agent_execution, "_provider", forbidden)
    monkeypatch.setattr(agent_execution, "open_task_pack_publication", forbidden)
    plan = _plan()
    with pytest.raises(KernelError) as error:
        await agent_execution.run_swebench_agent_case(
            plan=plan,
            case=plan.cases[0],
            run_root=tmp_path / "run",
            workspace=tmp_path,
            git_executable=Path("/usr/bin/git"),
            provider_config=OpenAIChatConfig(model="fixture"),
            execution_fingerprint="a" * 64,
            process_profile=_process_profile(tmp_path),
            api_key="offline",
            enable_definition_views=invalid,
        )
    assert error.value.code == "swebench_execution_config_invalid"
    assert not (tmp_path / "run").exists()
