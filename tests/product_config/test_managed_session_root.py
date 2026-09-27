"""默认产品真实Root的认证Session装配、重启、拒绝与全生命周期清理。"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, ThreadCreated
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config import server as product
from harnessix.product_config.runtime import build_provider_bundle
from harnessix.product_config.session_key import open_product_session_binding
from harnessix.product_config.session_key_store import load_session_key
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.product_config.conftest import write_config


def arguments(tmp_path, config, monkeypatch):
    monkeypatch.setenv("PRIMARY_API_KEY", "managed-root-primary-fixture")
    monkeypatch.setenv("BACKUP_API_KEY", "managed-root-backup-fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return dict(
        config_path=write_config(tmp_path / "config.json", config),
        profile_id=None,
        workspace=workspace,
        state_directory=tmp_path / "state",
        input_stream=io.BytesIO(),
        output_stream=io.BytesIO(),
    )


def ledger(path):
    with sqlite3.connect(path) as db:
        return {
            name: db.execute("SELECT * FROM " + name).fetchall()
            for name in (
                "agent_events",
                "agent_threads",
                "agent_event_publications",
                "agent_projection_publications",
                "agent_publication_store",
            )
        }


@pytest.mark.parametrize("after", [0, 1])
async def test_managed_history_reader_does_not_mix_concurrent_commit_generations(
    tmp_path, after, monkeypatch
):
    from uuid import uuid4

    from harnessix.agent.models import ThreadArchived
    from harnessix.session import sqlite_publication
    from tests.agent.test_publication import protected

    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    reached, release = asyncio.Event(), asyncio.Event()
    original_checkpoint = sqlite_publication.checkpoint
    reader = None

    async def paused_checkpoint(*args):
        proof = await original_checkpoint(*args)
        if asyncio.current_task() is reader:
            reached.set()
            await release.wait()
        return proof

    with protected() as scope:
        async with open_product_session_binding(root, scope) as binding:
            store = SQLiteSessionStore(root / "sessions.db", publication=binding)
            await store.initialize()
            tid = uuid4()
            initial = EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))
            await store.append(tid, [initial], expected_sequence=0)
            monkeypatch.setattr(sqlite_publication, "checkpoint", paused_checkpoint)
            async with asyncio.timeout(5):
                reader = asyncio.create_task(store.events(tid, after=after))
                await reached.wait()
                try:
                    # 读连接已固定原Checkpoint，另一真实连接在其取Event之前提交新版本。
                    await store.append(
                        tid, [EventDraft(payload=ThreadArchived(reason=None))], expected_sequence=1
                    )
                finally:
                    release.set()
                events = await reader
            assert [event.event_id for event in events] == (
                [initial.event_id] if after == 0 else []
            )
            assert len(await store.events(tid)) == 2
            assert (await store.get_thread(tid)).sequence == 2
            rows = ledger(store.path)
            assert len(rows["agent_events"]) == len(rows["agent_event_publications"]) == 2


def observe(monkeypatch):
    copies, providers, scopes = [], [], []
    original = product.open_product_session_binding

    @asynccontextmanager
    async def binding(*args):
        async with original(*args) as owned:
            copies.extend((owned._key, owned._events._key))
            yield owned

    async def build(snapshot, selection, secrets, *, audit):
        scopes.extend(secrets._materials.values())

        def factory(_definition, _profile, _key):
            provider = ScriptedProvider([answer("正式安全回答")])
            providers.append(provider)
            return provider

        return await build_provider_bundle(
            snapshot, selection, secrets, audit=audit, factory=factory
        )

    monkeypatch.setattr(product, "open_product_session_binding", binding)
    monkeypatch.setattr(product, "build_provider_bundle", build)
    return copies, providers, scopes


async def test_default_root_reopens_original_events_then_commits_new_protected_turn(
    tmp_path, config, monkeypatch
):
    params = arguments(tmp_path, config, monkeypatch)
    copies, providers, scopes = observe(monkeypatch)
    identities, original_events, threads = [], [], []

    async def drive(server, _input, _output):
        service = server.service
        assert service.store._publication is not None
        assert service.store is service.runtime.store is service.artifact_reader.session
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            if not threads:
                thread = await client.create_thread(str(params["workspace"]), request_id="create")
                threads.append(thread.thread_id)
            else:
                before = await service.store.events(threads[0])
                assert [e.model_dump_json() for e in before] == original_events
                assert (await service.store.get_thread(threads[0])).sequence == len(original_events)
            await client.start_turn(threads[0], "实现安全读取", request_id=str(len(identities)))
            async with asyncio.timeout(5):
                while service._tasks:
                    await asyncio.gather(*tuple(service._tasks.values()))
            assert (await client.get_thread(threads[0])).latest_turn.status == "completed"
            events = await service.store.events(threads[0])
            if not original_events:
                original_events.extend(e.model_dump_json() for e in events)
            material = load_session_key(params["state_directory"])
            try:
                identities.append((material.store_id, material.key_id))
            finally:
                material.close()
            rows = ledger(service.store.path)
            assert len(rows["agent_events"]) == len(rows["agent_event_publications"])
            assert len(rows["agent_threads"]) == len(rows["agent_projection_publications"]) == 1
        finally:
            await client.close()

    monkeypatch.setattr(product, "run_stdio", drive)
    await product.run_product_stdio(**params)
    await product.run_product_stdio(**params)
    assert len(identities) == 2 and identities[0] == identities[1]
    assert copies and all(not any(value) for value in copies)
    assert scopes and all(not any(value.value) for value in scopes)
    assert sum(len(p.requests) for p in providers) == 2


@pytest.mark.parametrize("existing_key", [False, True])
async def test_default_root_refuses_original_unproven_history_before_provider_or_protocol(
    tmp_path, config, monkeypatch, existing_key
):
    params = arguments(tmp_path, config, monkeypatch)
    root = params["state_directory"]
    root.mkdir(mode=0o700)
    if existing_key:
        load_session_key(root).close()
    legacy = SQLiteSessionStore(root / "sessions.db")
    await legacy.initialize()
    from uuid import uuid4

    await legacy.append(
        uuid4(),
        [EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))],
        expected_sequence=0,
    )
    original = ledger(legacy.path)
    copies, providers, scopes = observe(monkeypatch)

    async def drive(*_args):
        pytest.fail("未证明历史不得开放协议")

    monkeypatch.setattr(product, "run_stdio", drive)
    with pytest.raises(KernelError) as caught:
        await product.run_product_stdio(**params)
    assert caught.value.code == (
        "publication_history_unproven" if existing_key else "publication_key_unavailable"
    )
    assert ledger(legacy.path) == original and not providers and not scopes
    assert all(not any(value) for value in copies)
    if not existing_key:
        assert not (root / "session-auth/key.v1").exists()
    assert params["output_stream"].getvalue() == b""


@pytest.mark.parametrize("corruption", ["missing_key", "damaged_key", "snapshot_and_sha"])
async def test_default_root_rejects_damage_without_reauthorizing_original_history(
    tmp_path, config, monkeypatch, corruption
):
    params = arguments(tmp_path, config, monkeypatch)
    copies, providers, scopes = observe(monkeypatch)

    async def create(server, _input, _output):
        client = AgentClient(InProcessAgentTransport(server))
        await client.initialize()
        try:
            await client.create_thread(str(params["workspace"]), request_id="create")
        finally:
            await client.close()

    monkeypatch.setattr(product, "run_stdio", create)
    await product.run_product_stdio(**params)
    root = params["state_directory"]
    path = root / "sessions.db"
    original = ledger(path)
    key = root / "session-auth/key.v1"
    if corruption == "missing_key":
        key.unlink()
    elif corruption == "damaged_key":
        key.write_bytes(b"malformed-key-envelope")
    else:
        import hashlib

        with sqlite3.connect(path) as db:
            raw = json.loads(db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0])
            raw["workspace"] = (tmp_path / "forged").as_posix()
            changed = json.dumps(raw, separators=(",", ":"))
            db.execute(
                "UPDATE agent_threads SET snapshot_json=?,snapshot_sha256=?",
                (changed, hashlib.sha256(changed.encode()).hexdigest()),
            )
    corrupted = ledger(path)
    providers.clear()

    async def forbidden(*_args):
        pytest.fail("损坏事实不得开放协议")

    monkeypatch.setattr(product, "run_stdio", forbidden)
    with pytest.raises(KernelError) as caught:
        await product.run_product_stdio(**params)
    assert caught.value.code == (
        "publication_history_unproven"
        if corruption == "snapshot_and_sha"
        else "publication_key_unavailable"
    )
    assert ledger(path) == corrupted
    assert ledger(path)["agent_events"] == original["agent_events"]
    assert all(not any(value) for value in copies)
    assert all(not any(value.value) for value in scopes)
    assert not any(p.requests for p in providers)
    if corruption == "missing_key":
        assert not key.exists()
    elif corruption == "damaged_key":
        assert key.read_bytes() == b"malformed-key-envelope"
