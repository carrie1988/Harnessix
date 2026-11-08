"""在隔离的Harbor依赖环境验证适配器契约；普通产品环境可跳过。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytest.importorskip("harbor", minversion="0.25.0")

from harbor.models.agent.context import AgentContext

from integrations.harbor.harnessix_agent import HarnessixAgent
from integrations.harbor.workspace_modes import normalize_modes_command


def agent(tmp_path: Path, **options) -> HarnessixAgent:
    instance = HarnessixAgent(
        logs_dir=tmp_path, server_config="/opt/harnessix/product.json", **options
    )
    instance.context_id = uuid4()
    return instance


async def test_instruction_never_enters_shell_command_or_agent_log(tmp_path: Path) -> None:
    instance = agent(
        tmp_path,
        action_config="/opt/harnessix/actions.json",
        approval_mode="auto",
        model_relays="127.0.0.1:18001=172.17.0.1:18101",
    )
    instance.exec_as_agent = AsyncMock(return_value=SimpleNamespace(stdout="a" * 40 + "\n"))
    uploaded = []

    async def upload(source, target):
        uploaded.append((await asyncio.to_thread(Path(source).read_text), target))

    environment = SimpleNamespace(upload_file=upload)
    instruction = "PRIVATE-INSTRUCTION; $(touch /outside)"
    await instance.run(instruction, environment, AgentContext())
    assert uploaded[0][0] == instruction
    assert uploaded[0][1].startswith("/tmp/harnessix-input-")
    commands = [call.kwargs["command"] for call in instance.exec_as_agent.await_args_list]
    assert all(instruction not in command for command in commands)
    head, run, cleanup, patch, state = commands
    assert "read-tree HEAD" in head and head.count("write-tree") == 1
    assert "--trial-id" in run and "--state-directory" in run
    assert "--action-config /opt/harnessix/actions.json" in run
    assert "--approval-mode auto" in run
    assert "--model-relay 127.0.0.1:18001=172.17.0.1:18101" in run
    assert "--events-output " in run and "harnessix-events.jsonl" in run
    # 未声明外部边界时不向App Server断言边界。
    assert instance.exec_as_agent.await_args_list[1].kwargs["env"] is None
    assert cleanup.startswith("rm -f -- /tmp/harnessix-input-")
    assert 'GIT_INDEX_FILE="$scratch/index"' in patch and "mktemp -d" in patch
    assert f"diff --binary {'a' * 40} " in patch
    assert patch.endswith("exit $status") and "/patch.diff" in patch
    assert state.startswith("tar -czf ") and "harnessix-state.tar.gz" in state


async def test_external_boundary_is_asserted_only_for_the_app_server_run(tmp_path: Path) -> None:
    instance = agent(tmp_path, approval_mode="auto", external_boundary=True)
    instance.exec_as_agent = AsyncMock(return_value=SimpleNamespace(stdout="a" * 40 + "\n"))
    environment = SimpleNamespace(upload_file=AsyncMock())
    await instance.run("fix it", environment, AgentContext())
    calls = instance.exec_as_agent.await_args_list
    run = next(call for call in calls if "--trial-id" in call.kwargs["command"])
    assert run.kwargs["env"] == {"HARNESSIX_EXTERNAL_BOUNDARY": "1"}
    assert all("env" not in call.kwargs for call in calls if call is not run)


@pytest.mark.parametrize("body", [None, "", "not json", "[]"])
def test_missing_or_invalid_receipt_leaves_attribution_to_parser(
    tmp_path: Path, body: str | None
) -> None:
    instance = agent(tmp_path)
    if body is not None:
        (tmp_path / "harnessix-summary.json").write_text(body)
    context = AgentContext(metadata={"existing": "preserved"})
    instance.populate_context_post_run(context)
    assert context.metadata == {"existing": "preserved"}


def test_context_keeps_native_observations_and_unknown_cost(tmp_path: Path) -> None:
    instance = agent(tmp_path)
    receipt = {
        "spec_version": "harnessix.harbor-protocol-result/v1",
        "trial_id": str(instance.context_id),
        "thread_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "turn_status": "completed",
        "reason": "completed",
        "input_tokens": 100,
        "output_tokens": 40,
        "turn_terminal_confirmed": True,
    }
    (tmp_path / "harnessix-summary.json").write_text(json.dumps(receipt))
    context = AgentContext(metadata={"existing": "preserved"})
    instance.populate_context_post_run(context)
    assert context.n_input_tokens is None
    assert context.n_output_tokens is None
    assert context.metadata["harnessix"]["observed_input_tokens"] == 100
    assert context.metadata["harnessix"]["observed_output_tokens"] == 40
    assert context.n_cache_tokens is None
    assert context.cost_usd is None
    assert context.metadata["harnessix"]["cost_completeness"] == "unknown"
    assert context.metadata["existing"] == "preserved"
    assert context.metadata["harnessix"]["thread_id"] == receipt["thread_id"]
    assert context.metadata["harnessix"]["turn_id"] == receipt["turn_id"]
    assert context.metadata["harnessix"]["events_exported"] is False
    assert context.metadata["harnessix"]["external_boundary"] is False
    receipt["trial_id"] = str(uuid4())
    (tmp_path / "harnessix-summary.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="身份"):
        instance.populate_context_post_run(AgentContext())


@pytest.mark.parametrize(
    "options",
    [
        {"workspace": "/tmp", "state_directory": "/tmp/state"},
        {"server_config": "/app/product.json"},
        {"action_config": "/app/actions.json"},
        {"model_name": "silent-model-override"},
        {"version": "unverified-version"},
        {"max_tokens": 0},
    ],
)
def test_adapter_rejects_unsafe_paths_or_ignored_model_selection(tmp_path: Path, options) -> None:
    with pytest.raises(ValueError):
        HarnessixAgent(
            logs_dir=tmp_path, **{"server_config": "/opt/harnessix/product.json", **options}
        )


async def test_install_uploads_bundle_templates_runner_and_relay(tmp_path: Path) -> None:
    bundle, product = tmp_path / "bundle.tar.gz", tmp_path / "product.json"
    bundle.write_bytes(b"bundle")
    product.write_text("{}")
    instance = agent(tmp_path, install_bundle=str(bundle), server_config_template=str(product))
    instance.exec_as_agent = AsyncMock()
    instance.exec_as_root = AsyncMock()
    uploaded = []

    async def upload(source, target):
        uploaded.append((Path(source).name, target))

    await instance.install(SimpleNamespace(upload_file=upload))
    assert uploaded == [
        ("bundle.tar.gz", "/installed-agent/harnessix-bundle.tar.gz"),
        ("product.json", "/opt/harnessix/product.json"),
        ("protocol_runner.py", "/installed-agent/harnessix_protocol_runner.py"),
        ("model_relay.py", "/installed-agent/model_relay.py"),
    ]
    root_commands = [call.kwargs["command"] for call in instance.exec_as_root.await_args_list]
    assert root_commands[0].startswith("mkdir -p /opt && tar -xzf ")
    assert root_commands[1] == "mkdir -p /opt/harnessix"


@pytest.mark.parametrize("enabled", [False, True])
async def test_workspace_mode_normalization_runs_only_when_enabled(
    tmp_path: Path, enabled: bool
) -> None:
    instance = agent(tmp_path, workspace="/testbed", normalize_workspace_modes=enabled)
    instance.exec_as_agent = AsyncMock()
    instance.exec_as_root = AsyncMock()

    async def upload(source, target):
        pass

    await instance.install(SimpleNamespace(upload_file=upload))
    root_commands = [call.kwargs["command"] for call in instance.exec_as_root.await_args_list]
    assert (normalize_modes_command("/testbed") in root_commands) is enabled


@pytest.mark.parametrize(
    "options",
    [
        {"approval_mode": "always"},
        {"install_bundle": "/missing/bundle.tar.gz"},
        {"action_config_template": __file__},
        {"bundle_root": "relative"},
        {"external_boundary": "yes"},
        {"normalize_workspace_modes": "yes"},
    ],
)
def test_invalid_install_or_approval_options_fail_before_any_trial(
    tmp_path: Path, options: dict
) -> None:
    with pytest.raises(ValueError):
        agent(tmp_path, **options)


def test_harbor_product_config_declares_the_local_model_window() -> None:
    from integrations.harbor.harnessix_configs import CONTEXT_WINDOW_TOKENS, product_config
    from scripts.swebench_local_run import MODEL

    profile = json.loads(product_config(18002))["profiles"][0]
    assert profile["context_window_tokens"] == CONTEXT_WINDOW_TOKENS[MODEL] == 262_144
    other = json.loads(product_config(18002, model="other-model"))["profiles"][0]
    assert "context_window_tokens" not in other
