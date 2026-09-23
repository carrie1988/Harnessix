"""真实产品stdio子进程的正常关闭和受控硬退出。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from harnessix.product_config.action_store import SQLiteProductRuntimeConfigStore
from harnessix.product_config.codec import canonical_product_config_bytes
from harnessix.product_config.contracts import (
    EnvironmentSecretSourceConfig,
    ModelCapabilities,
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
)
from harnessix.sdk.agent_client import AgentClient, AgentSDKError
from harnessix.sdk.subprocess import SubprocessAgentTransport
from scripts.soak_restart_proof import SoakRestartChildResult


def _config(path: Path) -> Path:
    secret = SecretReference(name="soak-key", version="v1")
    config = ProductConfigV2(
        active_profile="soak",
        secret_sources=(
            EnvironmentSecretSourceConfig(secret=secret, environment_variable="HARNESSIX_SOAK_KEY"),
        ),
        providers=(
            ProviderDefinition(
                provider_id="soak",
                kind="openai_chat",
                base_url="https://api.openai.test/v1",
                credential=secret,
            ),
        ),
        profiles=(
            ModelProfile(
                profile_id="soak",
                provider_id="soak",
                model="soak-offline",
                capabilities=ModelCapabilities(),
            ),
        ),
    )
    path.write_bytes(canonical_product_config_bytes(config))
    path.chmod(0o600)
    return path


async def _wait_marker(path: Path) -> None:
    async with asyncio.timeout(20):
        while not path.is_file():  # noqa: ASYNC110, ASYNC240
            await asyncio.sleep(0.005)


def _client(config: Path, workspace: Path, state: Path, gate: Path) -> AgentClient:
    gate.mkdir(mode=0o700)
    command = (
        sys.executable,
        str(Path(__file__).parents[2] / "scripts" / "soak_restart_child.py"),
        str(config),
        str(workspace),
        str(state),
        str(gate),
    )
    return AgentClient(SubprocessAgentTransport(command))


async def test_real_product_restart_child_closes_and_hard_exits_without_turn(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    config = _config(tmp_path / "config.json")

    first = _client(config, workspace, state, tmp_path / "gate-one")
    try:
        async with asyncio.timeout(30):
            await first.initialize()
            thread = await first.create_thread(str(workspace), request_id="soak-first")
            page = await first.list_threads(limit=50)
        assert [item.thread_id for item in page.threads] == [thread.thread_id]
    finally:
        await first.close()
    result_path = tmp_path / "gate-one" / "child-result.json"
    result = SoakRestartChildResult.model_validate_json(result_path.read_bytes())
    assert result.rss.rss_bytes > 0
    with SQLiteProductRuntimeConfigStore(state / "product-config.db") as store:
        assert len(store.action_recovery_scans()) == 1
        assert len(store.action_recovery_reports()) == 1

    second_gate = tmp_path / "gate-two"
    second = _client(config, workspace, state, second_gate)
    try:
        async with asyncio.timeout(30):
            await second.initialize()
            page = await second.list_threads(limit=50)
        assert [item.thread_id for item in page.threads] == [thread.thread_id]
        (second_gate / "crash.request").write_bytes(b"CRASH\n")
        await _wait_marker(second_gate / "crash.ack")
        with pytest.raises(AgentSDKError) as error:
            async with asyncio.timeout(30):
                await second.list_threads(limit=50)
        assert error.value.code == "server_closed"
    finally:
        await second.close()
    assert (second_gate / "crash.ack").read_bytes() == b"ACK\n"
    assert not (second_gate / "child-result.json").exists()
    with SQLiteProductRuntimeConfigStore(state / "product-config.db") as store:
        assert [scan.owner_generation for scan in store.action_recovery_scans()] == [1, 2]
        assert len(store.action_recovery_reports()) == 2
