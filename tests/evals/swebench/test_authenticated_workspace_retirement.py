"""真实SDK/认证Session的退役恢复；模型及容器边界使用离线替身，不作质量证据。"""

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus, TurnStatusV18
from harnessix.evals.swebench import agent_execution, execution
from harnessix.evals.swebench.execution_contracts import build_swebench_case_execution_state
from harnessix.evals.swebench.execution_io import write_case_execution_state
from harnessix.evals.swebench.run_config import SweBenchRecyclingAgentRunConfig
from harnessix.evals.swebench.workspace import materialize_swebench_workspace
from harnessix.evals.swebench.workspace_retirement import (
    RETIRED_WORKSPACE_MARKER,
    read_retired_patch,
    seal_and_retire_workspace,
)
from harnessix.models._history import tool_alias
from harnessix.models.openai_chat import OpenAIChatProvider
from harnessix.product_config import action_runtime
from harnessix.product_config.process_profile import probe_product_process_profile
from tests.evals.swebench.test_execution import _config
from tests.evals.swebench.test_run_config import build_agent_test_plan
from tests.models.wire import WireStream, call, chunk, frame, response, text_frames
from tests.product_config.test_process_action import _probe_runner

KEY = "authenticated-retirement-offline-key"


def _private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _conflicting_calls():
    calls = [call(0), call(1)]
    calls[0]["function"]["name"] = tool_alias("run_profile.public-tests")
    calls[1]["function"]["name"] = "apply_patch_batch"
    return [
        frame(chunk({"tool_calls": calls})),
        frame(chunk(finish="tool_calls")),
        frame(chunk(usage=True)),
        b"data: [DONE]\n\n",
    ]


@pytest.mark.parametrize("mixed_calls", [False, True])
async def test_authenticated_terminal_seal_recovery_never_reexecutes_or_recreates(
    tmp_path, monkeypatch, mixed_calls
):
    # SDK接受多调用后仍必须由Kernel拒绝互斥组，不能靠Provider提前拒绝冒充验证。
    plan = build_agent_test_plan(parallel_tool_calls=True)
    config = SweBenchRecyclingAgentRunConfig.model_validate(
        {
            **_config(tmp_path, plan).model_dump(),
            "spec_version": "harnessix.swebench-agent-run-config/v9",
            "agent_strategy": "optional_pre_edit",
        }
    )
    root = Path(config.work_root)
    case = plan.cases[0]
    case_root = root / "cases" / "case-000"
    workspace = root / "workspaces" / case.instance_id
    for path in (
        root,
        root / "cases",
        case_root,
        case_root / "run",
        root / "workspaces",
        workspace,
    ):
        _private(path)
    (workspace / "source.py").write_text("answer = 42\n")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        frames = _conflicting_calls() if mixed_calls else text_frames()
        parts = []
        for part in frames:
            if part == b"data: [DONE]\n\n":
                parts.append(part)
            else:
                payload = json.loads(part.removeprefix(b"data: "))
                payload["model"] = config.provider_config.model
                parts.append(frame(payload))
        return response(WireStream(parts))

    # 正式装配/SQLite/Owner/SDK不替代；仅不触及真实容器与云模型。
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
        lambda provider_config, key: OpenAIChatProvider(
            provider_config, api_key=key, transport=httpx.MockTransport(handler)
        ),
    )
    arguments = dict(
        plan=plan,
        case=case,
        run_root=case_root / "run",
        workspace=workspace,
        git_executable=Path(config.git_executable),
        provider_config=config.provider_config,
        execution_fingerprint=config.fingerprint,
        process_profile=config.process_profile_for(case.instance_id),
        api_key=KEY,
        budget_pressure_compaction=config.budget_pressure_compaction,
        allow_optional_pre_edit_profile=config.optional_pre_edit_profile_enabled,
    )
    thread_id, turn = await agent_execution.run_swebench_agent_case(**arguments)
    assert len(requests) == 1 and turn.model_steps == 1
    assert turn.status is (TurnStatus.FAILED if mixed_calls else TurnStatus.COMPLETED)
    failure = execution._case_evidence_failure(
        turn, "", "run_profile.public-tests", cost_complete=True
    )
    assert failure[0] == (
        "swebench_model_response_invalid" if mixed_calls else "swebench_patch_missing"
    )
    state = build_swebench_case_execution_state(
        plan,
        case.instance_id,
        uuid4(),
        status="failed",
        thread_id=thread_id,
        turn_id=turn.turn_id,
        turn_status=TurnStatusV18(turn.status),
        patch_sha256=hashlib.sha256(b"").hexdigest(),
        input_tokens=turn.usage.input_tokens,
        output_tokens=turn.usage.output_tokens,
        error_code=failure[0],
    )
    state_path = case_root / "case-state.json"
    write_case_execution_state(state_path, plan, state)
    native = case_root / "run" / "session.sqlite"
    original = {path: path.read_bytes() for path in (state_path, native)}
    execution._require_case_evidence(state, thread_id, turn, "")
    manifest = seal_and_retire_workspace(plan, config, 0, state, "", api_key=KEY)
    assert manifest.thread_id == thread_id and manifest.turn_id == turn.turn_id
    assert list(workspace.iterdir()) == [workspace / RETIRED_WORKSPACE_MARKER]
    assert read_retired_patch(plan, config, 0, api_key=KEY) == ""

    def forbidden(*_args, **_kwargs):
        pytest.fail("原终态恢复不得重新创建工作区、开启Provider、Git或执行器")

    monkeypatch.setattr(agent_execution, "_provider", forbidden)
    monkeypatch.setattr(agent_execution, "CodingToolRuntime", forbidden)
    monkeypatch.setattr(agent_execution, "open_default_product_action_runtime", forbidden)
    monkeypatch.setattr(execution, "materialize_swebench_workspace", forbidden)
    monkeypatch.setattr(execution, "collect_workspace_patch", forbidden)
    restored_thread, restored_turn = await execution._open_case_turn(
        plan, config, case, case_root, workspace, arguments["process_profile"], KEY, CancelToken()
    )
    execution._require_case_evidence(state, restored_thread, restored_turn, manifest.patch)
    assert (restored_thread, restored_turn) == (thread_id, turn)
    assert {path: path.read_bytes() for path in original} == original
    assert len(requests) == 1
    with pytest.raises(KernelError) as error:
        materialize_swebench_workspace(
            case,
            workspaces_root=root / "workspaces",
            cache_root=Path(config.cache_root),
            git_executable=Path(config.git_executable),
            allow_network=False,
        )
    assert error.value.code == "swebench_workspace_retired"
    cache_path = Path(config.cache_root)
    assert not await asyncio.to_thread(cache_path.exists)
