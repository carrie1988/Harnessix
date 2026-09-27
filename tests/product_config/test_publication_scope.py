"""真实产品启动装配同一Provider材料与公开作用域；仅模型工厂及传输驱动替身。"""

from __future__ import annotations

import asyncio
import io
import sqlite3
from contextlib import asynccontextmanager

import pytest

from harnessix.agent.errors import KernelError
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.runtime import (
    build_provider_bundle,
    provider_secret_references,
    select_profile,
)
from harnessix.product_config.server import run_product_stdio
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY
from tests.artifacts.helpers import step
from tests.product_config.conftest import write_config


@pytest.mark.parametrize("case", ["safe", "leak", "text_leak", "startup_failure"])
async def test_product_root_owns_selected_provider_snapshot_and_clears_on_exit(
    tmp_path, config, monkeypatch, case
):
    monkeypatch.setenv("PRIMARY_API_KEY", CANARY)
    monkeypatch.setenv("BACKUP_API_KEY", "backup-original-model-key")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    lines = ["needle benign\n"] * 300
    if case == "leak":
        lines[149] = "needle " + CANARY + "\n"
    (workspace / "main.py").write_text("".join(lines))
    path = write_config(tmp_path / "config.json", config)
    state = tmp_path / "state"
    selected = []
    action_scopes = []
    held = []
    keys = []
    providers = []

    async def build(snapshot, selection, secrets, *, audit):
        selected.append(secrets)
        held.extend(secrets._materials.values())
        if case == "startup_failure":
            raise KernelError("product_provider_unavailable", "Provider构造失败")

        def factory(definition, profile, key):
            keys.append((definition.provider_id, key))
            monkeypatch.setenv("PRIMARY_API_KEY", "rotated-current-model-key")
            monkeypatch.setenv("BACKUP_API_KEY", "rotated-backup-model-key")
            provider = ScriptedProvider(
                [answer(CANARY)] if case == "text_leak" else [step(), answer("产品读取结束")]
            )
            providers.append(provider)
            return provider

        return await build_provider_bundle(
            snapshot, selection, secrets, audit=audit, factory=factory
        )

    async def drive(server, _input, _output):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            thread = await client.create_thread(str(workspace), request_id="create")
            await client.start_turn(thread.thread_id, "读取工作区", request_id="read")
            async with asyncio.timeout(5):
                while True:
                    snapshot = await client.get_thread(thread.thread_id)
                    if snapshot.latest_turn and snapshot.latest_turn.status in {
                        "failed",
                        "completed",
                    }:
                        break
                    await asyncio.sleep(0.01)
            assert snapshot.latest_turn.status == (
                "failed" if case in {"leak", "text_leak"} else "completed"
            )
            replay = await client.replay_events(thread.thread_id, limit=200)
            assert replay.events and CANARY not in replay.model_dump_json()
            assert len(providers[0].requests) == (1 if case in {"leak", "text_leak"} else 2)
        finally:
            await client.close()

    from harnessix.product_config.server import open_default_product_action_runtime

    @asynccontextmanager
    async def open_actions(*args, **kwargs):
        action_scopes.append(kwargs["output_redaction"])
        assert kwargs["output_redaction"] is selected[0]
        assert CANARY.encode() in kwargs["output_redaction"].output_redaction_values()
        async with open_default_product_action_runtime(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(
        "harnessix.product_config.server.open_default_product_action_runtime", open_actions
    )
    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    arguments = dict(
        config_path=path,
        profile_id=None,
        workspace=workspace,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    if case == "startup_failure":
        with pytest.raises(KernelError) as error:
            await run_product_stdio(**arguments)
        assert error.value.code == "product_provider_unavailable"
    else:
        await run_product_stdio(**arguments)
        assert action_scopes == [selected[0]]
        assert keys == [("primary", CANARY), ("backup", "backup-original-model-key")]
        assert all(CANARY.encode() not in p.read_bytes() for p in state.rglob("*.db*"))
        with sqlite3.connect(state / "sessions.db") as db:
            rows = db.execute(
                "SELECT publication_epoch, publication_policy FROM agent_artifacts"
            ).fetchall()
        assert len(rows) == (0 if case in {"leak", "text_leak"} else 1)
        if rows:
            assert rows[0][0] and rows[0][1] == "harnessix.public-output-protection/v1"
    assert selected and held and all(not any(m.value) for m in held)
    with pytest.raises(KernelError) as closed:
        selected[0].resolve("primary-api-key")
    assert closed.value.code == "trusted_action_secret_unavailable"


async def test_provider_reference_selection_does_not_capture_unused_profile(tmp_path, config):
    snapshot = load_product_config(write_config(tmp_path / "config.json", config))
    refs = provider_secret_references(snapshot, select_profile(snapshot, "backup"))
    assert [(r.name, r.version) for r in refs] == [("backup-api-key", "v2")]
    assert all(not hasattr(r, "target") for r in refs)


async def test_process_dependencies_receive_same_model_protection_without_injection(tmp_path):
    from harnessix.product_config.action_contracts import build_product_action_config
    from harnessix.product_config.action_runtime import _open_action_dependencies
    from tests.agent.test_publication import protected
    from tests.product_config.test_process_action import _fake_engine, _NoSecrets, _profile

    config = build_product_action_config(process_profiles=(_profile(_fake_engine(tmp_path)),))
    with protected() as scope:
        async with _open_action_dependencies(
            tmp_path / "state", _NoSecrets(), config, output_redaction=scope
        ) as dependencies:
            assert dependencies.supervisor is not None
            assert dependencies.supervisor._output_redaction is scope
            assert dependencies.supervisor._output_redaction.output_redaction_values() == (
                CANARY.encode(),
            )
            assert all(not item.verified for item in dependencies.probe_cache.values())
            # 实际Supervisor已装配；无真实引擎的Profile仍不进入执行目录或退回Host。
