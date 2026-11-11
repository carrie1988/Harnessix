"""Thread投影解析缓存（ADR 0133）：原字节不变时只解析一次，每次读取的存储侧校验一项不少。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemStarted,
    TextContent,
    Thread,
    ThreadArchived,
    TurnStarted,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.scripted import ScriptedProvider
from harnessix.session import sqlite as sqlite_module
from harnessix.session import sqlite_publication as publication_module
from harnessix.session.parsed_projection_cache import ParsedProjectionCache, parse_projection
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.session.test_authenticated_history import binding, seed


@pytest.fixture
def parses(monkeypatch):
    """记录每次Thread投影解析所用的原文本。"""
    calls: list[str] = []
    original = Thread.model_validate_json

    def counted(encoded, *args, **kwargs):
        calls.append(encoded)
        return original(encoded, *args, **kwargs)

    monkeypatch.setattr(Thread, "model_validate_json", counted)
    return calls


def rewrite_projection(path, body, digest=None):
    """直接改写投影正文；给出digest时同时改写行内摘要。"""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE agent_threads SET snapshot_json=?", (body,))
        if digest is not None:
            db.execute("UPDATE agent_threads SET snapshot_sha256=?", (digest,))


async def test_unchanged_projection_is_parsed_once_while_every_read_rechecks_bytes_and_seal(
    tmp_path, monkeypatch, parses
):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "state.db", proof)
        hashed: list[str] = []
        seals: list[int] = []
        original_seal = SessionPublicationBinding.verify_projection

        def digest(stage, body):
            hashed.append(stage)
            return hashlib.sha256(body)

        def verify_seal(self, *args):
            seals.append(1)
            return original_seal(self, *args)

        monkeypatch.setattr(
            publication_module, "hashlib", SimpleNamespace(sha256=lambda b: digest("auth", b))
        )
        monkeypatch.setattr(
            sqlite_module, "hashlib", SimpleNamespace(sha256=lambda b: digest("storage", b))
        )
        monkeypatch.setattr(SessionPublicationBinding, "verify_projection", verify_seal)

        first = await store.get_thread(tid)
        second = await store.get_thread(tid)
        third = await store.get_thread(tid)

        assert first == expected
        assert second is first and third is first
        assert len(parses) == 1
        assert hashed == ["auth", "storage"] * 3
        assert len(seals) == 3


async def test_each_committed_projection_is_parsed_once_by_the_reads_that_follow(tmp_path, parses):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        await store.get_thread(tid)
        parses.clear()
        turn_id = uuid4()
        started = await store.append(
            tid,
            [
                EventDraft(
                    turn_id=turn_id,
                    payload=TurnStarted(
                        request_id="fixture", request_fingerprint="1" * 64, budget=Budget()
                    ),
                )
            ],
            expected_sequence=1,
        )
        # 追加在事务内读取的是已缓存的旧投影；新投影要等下一次读取才解析。
        assert parses == []
        assert await store.get_thread(tid) == started
        item = EventDraft(
            turn_id=turn_id,
            payload=ItemStarted(
                item_id=uuid4(), content=TextContent(kind="user_message", text="fact")
            ),
        )
        appended = await store.append(tid, [item], expected_sequence=2)
        assert await store.get_thread(tid) == appended
        assert await store.get_thread(tid) == replay(await store.events(tid))
        assert len(parses) == 2


async def test_cached_thread_never_answers_for_bytes_the_seal_does_not_cover(tmp_path, parses):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "state.db", proof)
        assert await store.get_thread(tid) == expected
        forged = expected.model_copy(update={"workspace": "/forged"}).model_dump_json()
        rewrite_projection(store.path, forged, hashlib.sha256(forged.encode()).hexdigest())

        with pytest.raises(KernelError) as caught:
            await store.get_thread(tid)

        assert caught.value.code == "publication_history_unproven"
        assert len(parses) == 1


async def test_unauthenticated_store_follows_rewritten_bytes_and_still_rejects_corruption(
    tmp_path, parses
):
    store, tid, expected = await seed(tmp_path / "legacy.db", None)
    assert await store.get_thread(tid) == expected
    moved = expected.model_copy(update={"workspace": "/moved"})
    body = moved.model_dump_json()
    rewrite_projection(store.path, body, hashlib.sha256(body.encode()).hexdigest())

    # 无认证Store只有行内摘要，一致的改写按原设计被接受；返回的必须是新字节的解析结果。
    assert await store.get_thread(tid) == moved
    assert len(parses) == 2

    rewrite_projection(store.path, body.replace("/moved", "/mowed"))
    for _ in range(2):
        with pytest.raises(KernelError) as caught:
            await store.get_thread(tid)
        assert caught.value.code == "projection_corrupt"
    assert len(parses) == 2


async def test_structurally_invalid_projection_fails_on_every_read_and_is_not_cached(
    tmp_path, parses
):
    store, tid, _ = await seed(tmp_path / "legacy.db", None)
    rewrite_projection(store.path, "{}", hashlib.sha256(b"{}").hexdigest())

    for _ in range(2):
        with pytest.raises(KernelError, match="快照结构损坏") as caught:
            await store.get_thread(tid)
        assert caught.value.code == "projection_corrupt"

    assert parses == ["{}", "{}"]


async def test_startup_recovery_scan_parses_every_projection_without_the_cache(tmp_path, parses):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        await store.append(
            tid, [EventDraft(payload=ThreadArchived(reason="done"))], expected_sequence=1
        )
        await store.get_thread(tid)
        parses.clear()

        assert await store.recovery_threads() == ()

        assert len(parses) == 2


def test_cache_keeps_one_projection_per_thread_and_evicts_the_least_recently_used():
    cache = ParsedProjectionCache(max_threads=2)
    threads = {name: uuid4() for name in "abc"}
    parsed = {
        name: Thread.model_validate_json(
            Thread(
                thread_id=thread_id,
                workspace="/w",
                sequence=1,
                created_at="2026-11-11T00:00:00Z",
                updated_at="2026-11-11T00:00:00Z",
            ).model_dump_json()
        )
        for name, thread_id in threads.items()
    }
    cache.record(threads["a"], "1" * 64, 22, parsed["a"])
    cache.record(threads["b"], "2" * 64, 22, parsed["b"])

    assert cache.parsed(threads["a"], "1" * 64, 22) is parsed["a"]
    assert cache.parsed(threads["a"], "9" * 64, 22) is None
    assert cache.parsed(threads["a"], "1" * 64, 21) is None

    cache.record(threads["c"], "3" * 64, 22, parsed["c"])
    # a刚被读取过，淘汰的是最久未用的b。
    assert cache.parsed(threads["b"], "2" * 64, 22) is None
    assert cache.parsed(threads["a"], "1" * 64, 22) is parsed["a"]

    cache.record(threads["a"], "4" * 64, 22, parsed["c"])
    assert cache.parsed(threads["a"], "1" * 64, 22) is None
    assert cache.parsed(threads["a"], "4" * 64, 22) is parsed["c"]


def test_parse_without_a_cache_returns_a_fresh_thread_each_time():
    body = Thread(
        thread_id=uuid4(),
        workspace="/w",
        sequence=1,
        created_at="2026-11-11T00:00:00Z",
        updated_at="2026-11-11T00:00:00Z",
    ).model_dump_json()
    digest = hashlib.sha256(body.encode()).hexdigest()
    thread_id = uuid4()

    first = parse_projection(None, thread_id, body, digest, 22)
    second = parse_projection(None, thread_id, body, digest, 22)

    assert first == second and first is not second


async def test_tool_turn_parses_no_more_projections_than_it_commits(tmp_path, parses):
    """评测中每步宿主耗时随步数线性增长：每次读取都把整份投影解析两遍，一步有五十多次读取。"""
    with binding() as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        appends: list[int] = []
        original_append = store.append

        async def counted_append(*args, **kwargs):
            appends.append(1)
            return await original_append(*args, **kwargs)

        store.append = counted_append  # type: ignore[method-assign]
        steps = 5
        provider = ScriptedProvider([*(tool_step("test.read") for _ in range(steps)), answer()])
        async with AgentRuntime(store, provider, RecordingTools()) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.run_turn(thread.thread_id, "read", request_id="read-turn")

        assert turn.status == "completed" and turn.model_steps == steps + 1
        assert 0 < len(parses) <= len(appends)
        assert await store.get_thread(thread.thread_id) == replay(
            await store.events(thread.thread_id)
        )
