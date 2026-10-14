"""调查收尾策略经正式SWE端口传入SDK与认证Session；不是编码质量验收。"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.evals.swebench import agent_execution, execution
from harnessix.evals.swebench.run_config import SweBenchClosingAgentRunConfig
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.product_config import action_runtime
from harnessix.product_config.process_profile import probe_product_process_profile
from tests.evals.swebench.test_agent_execution import _process_profile
from tests.evals.swebench.test_execution import _config, _plan, _turn
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames
from tests.product_config.test_process_action import _probe_runner


@pytest.mark.parametrize("enabled", [False, True])
async def test_frozen_closing_phase_policy_reaches_case_entrypoint(tmp_path, monkeypatch, enabled):
    plan = _plan()
    original = _config(tmp_path, plan)
    config = (
        SweBenchClosingAgentRunConfig.model_validate(
            {**original.model_dump(), "spec_version": "harnessix.swebench-agent-run-config/v11"}
        )
        if enabled
        else original
    )
    calls = []

    async def run_case(**arguments):
        calls.append(arguments)
        return _turn(0).turn_id, _turn(0)

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
    assert calls[0]["reserve_closing_phase"] is enabled
    assert calls[0]["enable_grep_snapshots"] is False
    assert calls[0]["budget_pressure_compaction"] is enabled
    assert calls[0]["execution_fingerprint"] == config.fingerprint


@pytest.mark.parametrize("tight_ledger", [False, True])
@pytest.mark.parametrize("focused_followup", [False, True])
async def test_real_product_owner_context_catalog_and_sdk_publish_closing_phase(
    tmp_path, monkeypatch, tight_ledger, focused_followup
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "run").mkdir(mode=0o700)
    (workspace / "source.py").write_text("before\ndef choose(value): return value\nafter\n")
    plan = _plan()
    requests = []
    plan_call = call(0, json.dumps({"path": "source.py", "max_lines": 3}))
    plan_call["function"]["name"] = "read_file"
    first_usage = chunk(usage=True)
    if tight_ledger:
        first_usage["usage"] = {"prompt_tokens": 8000, "completion_tokens": 2, "total_tokens": 8002}
    frames = [
        frame(chunk({"tool_calls": [plan_call]})),
        frame(chunk(finish="tool_calls")),
        frame(first_usage),
        b"data: [DONE]\n\n",
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            output_frames = frames
        elif len(requests) == 2 and focused_followup:
            # 读取同文件相关定义，不因为首次已有完整SHA就认定语义上下文足够。
            followup = call(
                0, json.dumps({"path": "source.py", "anchor": "def choose", "max_lines": 1})
            )
            followup["function"]["name"] = "read_file"
            output_frames = [
                frame(chunk({"tool_calls": [followup]})),
                frame(chunk(finish="tool_calls")),
                frame(first_usage),
                b"data: [DONE]\n\n",
            ]
        else:
            output_frames = text_frames()
        return response(WireStream(output_frames))

    monkeypatch.setenv("CLOSING_PHASE_OFFLINE_KEY", "offline-placeholder")
    config = OpenAIChatConfig(
        model="test-model",
        api_key_env="CLOSING_PHASE_OFFLINE_KEY",
        max_attempts=1,
        capabilities=ChatCapabilities(tool_calls=True),
    )
    # 只替代容器环境探测，沿正式装配端口；本测试不执行容器或Profile。
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
        lambda *_: OpenAIChatProvider(config, transport=httpx.MockTransport(handler)),
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
        budget_pressure_compaction=True,
        reserve_closing_phase=True,
    )
    assert turn.status == TurnStatus.COMPLETED and thread_id is not None
    expected_calls = 3 if focused_followup else 2
    assert len(requests) == expected_calls and turn.model_steps == expected_calls
    if tight_ledger:
        assert [t["function"]["name"] for t in requests[1]["tools"]] == [
            "read_file",
            "apply_patch_batch",
        ]
        assert "tool_choice" not in requests[1]  # auto:模型可诚实结束而不是被强迫修改。
        assert (
            workspace / "source.py"
        ).read_text() == "before\ndef choose(value): return value\nafter\n"
    else:
        assert "read_file" in {t["function"]["name"] for t in requests[1]["tools"]}
    grep = next(
        tool["function"] for tool in requests[0]["tools"] if tool["function"]["name"] == "grep"
    )
    assert grep["parameters"]["properties"]["context_lines"]["default"] == 0
    assert "match_mode" not in grep["parameters"]["properties"]
    assert "grep is not regex" in json.dumps(requests[0]["messages"])
    assert "harnessix.coding-closing-preference/v2" in json.dumps(requests[0]["messages"])
    result = next(message for message in requests[1]["messages"] if message["role"] == "tool")
    output = json.loads(result["content"])["output"]
    assert output["digest_status"] == "complete" and len(output["content_sha256"]) == 64
    if focused_followup:
        results = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
        assert len(results) == 2
        reread = json.loads(results[-1]["content"])["output"]
        assert reread["text"] == "def choose(value): return value\n"
        assert reread["content_sha256"] == output["content_sha256"]
        if tight_ledger:
            assert "grep" not in {t["function"]["name"] for t in requests[-1]["tools"]}
    assert turn.budget == agent_execution._turn_budget(plan)
    assert (tmp_path / "run" / "session.sqlite").is_file()


@pytest.mark.parametrize("invalid", [None, 1, "true"])
async def test_invalid_closing_phase_flag_fails_before_opening_any_product_state(
    tmp_path, monkeypatch, invalid
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法调查收尾开关不得开启Provider或发布状态")

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
            reserve_closing_phase=invalid,
        )
    assert error.value.code == "swebench_execution_config_invalid"
    assert not (tmp_path / "run").exists()
