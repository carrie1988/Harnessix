"""Mock模型经正式SWE装配验证工作集视图；不执行模型API、容器或真实评测。"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.context.tool_result_contracts import (
    STALE_TOOL_RESULT_VERSION,
    ModelHistoryInspectionV3,
)
from harnessix.evals.swebench import agent_execution, execution
from harnessix.evals.swebench.run_config import SweBenchWorkingSetAgentRunConfig
from harnessix.models.config import ChatCapabilities, OpenAIChatConfig
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.product_config import action_runtime
from harnessix.product_config.process_profile import probe_product_process_profile
from tests.evals.swebench.test_agent_execution import _process_profile
from tests.evals.swebench.test_execution import _config, _plan, _turn
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames
from tests.product_config.test_process_action import _probe_runner


@pytest.mark.parametrize("groups", [None, 2])
async def test_frozen_working_set_policy_reaches_case_entrypoint(tmp_path, monkeypatch, groups):
    plan = _plan()
    original = _config(tmp_path, plan)
    config = (
        original
        if groups is None
        else SweBenchWorkingSetAgentRunConfig.model_validate(
            {
                **original.model_dump(),
                "spec_version": "harnessix.swebench-agent-run-config/v13",
                "retain_recent_result_groups": groups,
            }
        )
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
    assert calls[0]["working_set_result_groups"] == groups
    assert calls[0]["execution_fingerprint"] == config.fingerprint
    assert calls[0]["enable_definition_views"] is False


def read_frames(index: int) -> list[bytes]:
    arguments = {"path": "source.py", "start_line": 1 + index * 40, "max_lines": 40}
    read_call = call(0, json.dumps(arguments))
    read_call["function"]["name"] = "read_file"
    return [
        frame(chunk({"tool_calls": [read_call]})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]


@pytest.mark.parametrize("groups", [None, 1])
async def test_real_agent_sends_stale_placeholders_only_when_enabled(tmp_path, monkeypatch, groups):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "run").mkdir(mode=0o700)
    (workspace / "source.py").write_text(
        "".join(f"value_{i} = {i}  # line {i}\n" for i in range(120))
    )
    plan = _plan()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        index = len(requests) - 1
        return response(WireStream(read_frames(index) if index < 3 else text_frames()))

    monkeypatch.setenv("WORKING_SET_OFFLINE_KEY", "offline-placeholder")
    config = OpenAIChatConfig(
        model="test-model",
        api_key_env="WORKING_SET_OFFLINE_KEY",
        max_attempts=1,
        capabilities=ChatCapabilities(tool_calls=True),
    )
    # 只替代容器探测和模型网络；认证Session、Context、Catalog、read_file均走正式实现。
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
    _, turn = await agent_execution.run_swebench_agent_case(
        plan=plan,
        case=plan.cases[0],
        run_root=tmp_path / "run",
        workspace=workspace,
        git_executable=Path("/usr/bin/git"),
        provider_config=config,
        execution_fingerprint="a" * 64,
        process_profile=_process_profile(tmp_path),
        api_key="offline-placeholder",
        budget_pressure_compaction=groups is not None,
        working_set_result_groups=groups,
    )
    assert turn.status == TurnStatus.COMPLETED and len(requests) == 4
    results = [
        json.loads(message["content"])["output"]
        for message in requests[-1]["messages"]
        if message["role"] == "tool"
    ]
    guidance = "harnessix.coding-working-set/v1" in json.dumps(requests[0]["messages"])
    assert guidance is (groups is not None)
    if groups is None:
        assert all("text" in output for output in results)
        return
    stale, kept = results[:2], results[2]
    assert all(output["spec_version"] == STALE_TOOL_RESULT_VERSION for output in stale)
    assert all(output["retained"]["path"] == "source.py" for output in stale)
    assert all(len(output["retained"]["content_sha256"]) == 64 for output in stale)
    assert "value_80" in kept["text"]
    assert all(isinstance(i, ModelHistoryInspectionV3) for i in turn.model_history_inspections)


@pytest.mark.parametrize("invalid", [0, 65, True, "2"])
async def test_invalid_working_set_count_fails_before_provider_or_publication(
    tmp_path, monkeypatch, invalid
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法工作集配置不得开启Provider或发布状态")

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
            working_set_result_groups=invalid,
        )
    assert error.value.code == "swebench_execution_config_invalid"
    assert not (tmp_path / "run").exists()
