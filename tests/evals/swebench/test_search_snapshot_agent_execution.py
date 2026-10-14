"""搜索快照开关经正式SWE端口传入SDK和认证Session；不作编码质量验收。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.evals.swebench import agent_execution, execution
from harnessix.evals.swebench.run_config import SweBenchSearchSnapshotAgentRunConfig
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.product_config import action_runtime
from harnessix.product_config.process_profile import probe_product_process_profile
from tests.evals.swebench.test_agent_execution import _process_profile
from tests.evals.swebench.test_execution import _config, _plan, _turn
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames
from tests.product_config.test_process_action import _probe_runner


@pytest.mark.parametrize("enabled", [False, True])
async def test_frozen_search_snapshot_policy_reaches_case_entrypoint(
    tmp_path, monkeypatch, enabled
):
    plan = _plan()
    original = _config(tmp_path, plan)
    config = (
        SweBenchSearchSnapshotAgentRunConfig.model_validate(
            {**original.model_dump(), "spec_version": "harnessix.swebench-agent-run-config/v7"}
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
    assert calls[0]["enable_grep_snapshots"] is enabled
    assert calls[0]["budget_pressure_compaction"] is enabled
    assert calls[0]["execution_fingerprint"] == config.fingerprint


async def test_real_product_owner_context_catalog_and_sdk_publish_search_snapshot(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "run").mkdir(mode=0o700)
    (workspace / "source.py").write_text("before\nneedle = 42\nafter\n")
    plan = _plan()
    requests = []
    plan_call = call(0, '{"query":"needle","include":"*.py"}')
    plan_call["function"]["name"] = "grep"
    frames = [
        frame(chunk({"tool_calls": [plan_call]})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return response(WireStream(frames if len(requests) == 1 else text_frames()))

    monkeypatch.setenv("SEARCH_SNAPSHOT_OFFLINE_KEY", "offline-placeholder")
    config = OpenAIChatConfig(
        model="test-model",
        api_key_env="SEARCH_SNAPSHOT_OFFLINE_KEY",
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
        enable_grep_snapshots=True,
    )
    assert turn.status == TurnStatus.COMPLETED and thread_id is not None
    assert len(requests) == 2 and turn.model_steps == 2
    grep = next(
        tool["function"] for tool in requests[0]["tools"] if tool["function"]["name"] == "grep"
    )
    assert grep["parameters"]["properties"]["context_lines"]["default"] == 2
    result = next(message for message in requests[1]["messages"] if message["role"] == "tool")
    hit = json.loads(result["content"])["output"]["preview"]["matches"][0]
    assert (
        hit["content_sha256"] == hashlib.sha256((workspace / "source.py").read_bytes()).hexdigest()
    )
    assert hit["revision"] != hit["content_sha256"] and len(hit["context"]["lines"]) == 3
    assert "harnessix.coding-search-snapshots/v1" in json.dumps(requests[0]["messages"])
    assert turn.budget == agent_execution._turn_budget(plan)
    assert (tmp_path / "run" / "session.sqlite").is_file()


@pytest.mark.parametrize("invalid", [None, 1, "true"])
async def test_invalid_search_snapshot_flag_fails_before_opening_any_product_state(
    tmp_path, monkeypatch, invalid
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法搜索快照开关不得开启Provider或发布状态")

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
            enable_grep_snapshots=invalid,
        )
    assert error.value.code == "swebench_execution_config_invalid"
    assert not (tmp_path / "run").exists()
