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


def agent(tmp_path: Path, **options) -> HarnessixAgent:
    instance = HarnessixAgent(
        logs_dir=tmp_path, server_config="/opt/harnessix/product.json", **options
    )
    instance.context_id = uuid4()
    return instance


async def test_instruction_never_enters_shell_command_or_agent_log(tmp_path: Path) -> None:
    instance = agent(tmp_path, action_config="/opt/harnessix/actions.json")
    instance.exec_as_agent = AsyncMock()
    uploaded = []

    async def upload(source, target):
        uploaded.append((await asyncio.to_thread(Path(source).read_text), target))

    environment = SimpleNamespace(upload_file=upload)
    instruction = "PRIVATE-INSTRUCTION; $(touch /outside)"
    await instance.run(instruction, environment, AgentContext())
    assert uploaded[0][0] == instruction
    assert uploaded[0][1].startswith("/tmp/harnessix-input-")
    calls = instance.exec_as_agent.await_args_list
    assert len(calls) == 2
    assert all(instruction not in call.kwargs["command"] for call in calls)
    assert "--trial-id" in calls[0].kwargs["command"]
    assert "--state-directory" in calls[0].kwargs["command"]
    assert "--action-config /opt/harnessix/actions.json" in calls[0].kwargs["command"]
    assert calls[1].kwargs["command"].startswith("rm -f -- /tmp/harnessix-input-")


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
