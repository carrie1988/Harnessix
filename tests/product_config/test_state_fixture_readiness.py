"""真实产品状态夹具：观测等待不得取消正在提交或请求模型的后台Turn。"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from harnessix.agent.models import ModelHistoryPrepared
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.product_config.test_product_state_backup import complete_state


@pytest.mark.parametrize("phase", ["model_history_commit", "provider_stream"])
async def test_state_fixture_waits_for_public_completion_without_cancelling_owner(
    tmp_path, config, monkeypatch, phase
):
    reached, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    connection = None
    original_append = SQLiteSessionStore._append_in_transaction
    original_commit = aiosqlite.Connection.commit
    original_stream = ScriptedProvider.stream

    async def pause():
        reached.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def append(store, database, thread_id, batch, *, expected_sequence):
        nonlocal connection
        result = await original_append(
            store, database, thread_id, batch, expected_sequence=expected_sequence
        )
        if not reached.is_set() and any(
            isinstance(draft.payload, ModelHistoryPrepared) for draft in batch
        ):
            connection = database
        return result

    async def commit(database):
        if database is connection and not reached.is_set():
            await pause()
        await original_commit(database)

    async def stream(provider, request, cancel):
        if not reached.is_set():
            await pause()
        async for event in original_stream(provider, request, cancel):
            yield event

    if phase == "model_history_commit":
        monkeypatch.setattr(SQLiteSessionStore, "_append_in_transaction", append)
        monkeypatch.setattr(aiosqlite.Connection, "commit", commit)
    else:
        monkeypatch.setattr(ScriptedProvider, "stream", stream)
    # 直接执行同一夹具；保留真实配置、认证Session、Artifact和所有完成断言。
    setup = asyncio.create_task(complete_state.__wrapped__(tmp_path, config, monkeypatch))
    try:
        await asyncio.wait_for(reached.wait(), timeout=15)
        # 超过原夹具的五秒观察窗口，但远低于产品正式120秒Turn预算。
        await asyncio.sleep(5.2)
        assert not cancelled.is_set(), "夹具的观测超时取消了后台Turn"
        assert not setup.done(), "夹具未等待正式完成事实"
        release.set()
        root, prepared = await asyncio.wait_for(asyncio.shield(setup), timeout=20)
        assert (root / "sessions.db").is_file()
        assert prepared.blobs
    finally:
        release.set()
        if not setup.done():
            setup.cancel()
        await asyncio.gather(setup, return_exceptions=True)
