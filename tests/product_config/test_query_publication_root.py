"""默认产品Root直接Service查询，真实配置/Scope/SQLite；仅模型工厂与终端驱动替身。"""

from __future__ import annotations

import io

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.service import AgentServiceError
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.contracts import SecretReference
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.product_config.server import run_product_stdio
from harnessix.product_config.session_key import open_product_session_binding
from harnessix.protocol.contracts import (
    EventsNextParams,
    EventsReplayParams,
    ThreadGetParams,
    ThreadListParams,
    ThreadResumeParams,
)
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
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
    state.mkdir(mode=0o700)
    old_provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("old", "1", "OLD"),),
        environment={"OLD": "different-original-provider-material"},
    )
    with SecretPublicationScope(
        (SecretReference(name="old", version="1"),), old_provider
    ) as old_scope:
        async with open_product_session_binding(state, old_scope) as binding:
            sessions = SQLiteSessionStore(state / "sessions.db", publication=binding)
            async with AgentRuntime(sessions, ScriptedProvider([answer(CANARY)])) as original:
                thread = await original.create_thread(str(workspace / CANARY))
                await original.run_turn(thread.thread_id, "safe", request_id="original")
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
    # 原Scope下已证明的历史不授予当前材料公开许可；原字节不得清洗或重签。
    assert CANARY.encode() in (state / "sessions.db").read_bytes()
