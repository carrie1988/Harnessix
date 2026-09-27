"""默认产品Root直接Service查询，真实配置/Scope/SQLite；仅模型工厂与终端驱动替身。"""

from __future__ import annotations

import io

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.service import AgentServiceError
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.product_config.server import run_product_stdio
from harnessix.protocol.contracts import (
    EventsNextParams,
    EventsReplayParams,
    ThreadGetParams,
    ThreadListParams,
    ThreadResumeParams,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY
from tests.product_config.conftest import write_config


async def test_default_root_guards_exported_queries_without_transport_proxy(
    tmp_path, config, monkeypatch
):
    monkeypatch.setenv("PRIMARY_API_KEY", CANARY)
    monkeypatch.setenv("BACKUP_API_KEY", "backup-original-model-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    sessions = SQLiteSessionStore(state / "sessions.db")
    async with AgentRuntime(sessions, ScriptedProvider([answer(CANARY)])) as legacy:
        thread = await legacy.create_thread(str(workspace / CANARY))
        await legacy.run_turn(thread.thread_id, "safe", request_id="legacy")
    path = write_config(tmp_path / "config.json", config)
    providers, held, scopes = [], [], []
    codes = []

    async def build(snapshot, selection, secrets, *, audit):
        scopes.append(secrets)
        held.extend(secrets._materials.values())

        def factory(_definition, _profile, _key):
            provider = ScriptedProvider([])
            providers.append(provider)
            return provider

        return await build_provider_bundle(
            snapshot, selection, secrets, audit=audit, factory=factory
        )

    async def drive(server, _input, _output):
        service = server.service
        assert service.store is service.runtime.store
        assert service.artifact_reader.session is service.store
        before = await service.store.get_thread(thread.thread_id)
        calls = (
            (service.get_thread, ThreadGetParams(thread_id=thread.thread_id)),
            (service.list_threads, ThreadListParams()),
            (service.resume_thread, ThreadResumeParams(thread_id=thread.thread_id)),
            (service.replay_events, EventsReplayParams(thread_id=thread.thread_id)),
            (service.next_events, EventsNextParams(thread_id=thread.thread_id, wait_ms=0)),
        )
        try:
            for function, params in calls:
                with pytest.raises(AgentServiceError) as caught:
                    await function(params)
                codes.append(caught.value.code)
                assert CANARY not in str(caught.value)
                assert await service.store.get_thread(thread.thread_id) == before
            assert not service._tasks
        finally:
            await server.close()

    monkeypatch.setattr("harnessix.product_config.server.build_provider_bundle", build)
    monkeypatch.setattr("harnessix.product_config.server.run_stdio", drive)
    await run_product_stdio(
        config_path=path,
        profile_id=None,
        workspace=workspace,
        state_directory=state,
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )
    assert codes == ["public_output_secret_leak"] * 5
    assert scopes and held and all(not any(material.value) for material in held)
    assert providers and all(not provider.requests for provider in providers)
    # 既有私有历史故意包含合成材料；公开拒绝不能清洗、删除或重新授权它。
    assert CANARY.encode() in (state / "sessions.db").read_bytes()
