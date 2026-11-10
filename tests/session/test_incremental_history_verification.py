"""会话历史增量校验（ADR 0126）：游标读取只校验本次返回的事件，交付的数据仍全部经过认证。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, EventDraft, ItemStarted, TextContent, TurnStarted
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.session.verified_prefix_cache import VerifiedPrefix, VerifiedPrefixCache
from tests.session.test_authenticated_history import binding, seed


async def history(tmp_path, proof, items=6):
    """ThreadCreated、TurnStarted与若干用户消息Item组成的认证历史。"""
    store, tid, _ = await seed(tmp_path / "state.db", proof)
    turn_id = uuid4()
    drafts = [
        EventDraft(
            turn_id=turn_id,
            payload=TurnStarted(
                request_id="fixture", request_fingerprint="1" * 64, budget=Budget()
            ),
        ),
        *(
            EventDraft(
                turn_id=turn_id,
                payload=ItemStarted(
                    item_id=uuid4(), content=TextContent(kind="user_message", text=f"fact {i}")
                ),
            )
            for i in range(items)
        ),
    ]
    await store.append(tid, drafts, expected_sequence=1)
    return store, tid


@pytest.fixture
def verifications(monkeypatch):
    """统计逐事件MAC校验次数。"""
    calls: list[int] = []
    original = SessionPublicationBinding.verify_event

    def counted(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SessionPublicationBinding, "verify_event", counted)
    return calls


def tamper(path, sequence):
    with closing(sqlite3.connect(path)) as db, db:
        raw = db.execute("SELECT event_json FROM agent_events WHERE sequence=?", (sequence,))
        body = raw.fetchone()[0]
        db.execute("UPDATE agent_events SET event_json=? WHERE sequence=?", (body + " ", sequence))


async def test_cursor_reads_verify_only_the_events_they_return(tmp_path, verifications):
    with binding() as proof:
        store, tid = await history(tmp_path, proof)
        full = await store.events(tid)
        assert len(verifications) == len(full) == 8
        verifications.clear()
        assert await store.events(tid, after=5) == full[5:]
        assert len(verifications) == 3
        verifications.clear()
        await store.append(
            tid,
            [
                EventDraft(
                    turn_id=full[-1].turn_id,
                    payload=ItemStarted(
                        item_id=uuid4(), content=TextContent(kind="user_message", text="new")
                    ),
                )
            ],
            expected_sequence=len(full),
        )
        verifications.clear()
        latest = await store.events(tid, after=len(full))
        assert [event.sequence for event in latest] == [len(full) + 1]
        assert len(verifications) == 1


async def test_a_tampered_event_that_would_be_returned_is_rejected(tmp_path):
    with binding() as proof:
        store, tid = await history(tmp_path, proof)
        await store.events(tid)
        tamper(store.path, 7)
        with pytest.raises(KernelError) as caught:
            await store.events(tid, after=5)
        assert caught.value.code == "publication_history_unproven"


async def test_an_earlier_tampered_event_is_caught_by_the_next_full_read(tmp_path):
    """ADR 0126的边界：已验证且本次不返回的旧事件，在游标读取时不重验，由下一次全量读取发现。"""
    with binding() as proof:
        store, tid = await history(tmp_path, proof)
        full = await store.events(tid)
        tamper(store.path, 2)
        # 返回的事件全部重新验过MAC，链与签名检查点一致：交付的数据仍可信。
        assert await store.events(tid, after=5) == full[5:]
        with pytest.raises(KernelError) as caught:
            await store.events(tid)
        assert caught.value.code == "publication_history_unproven"


async def test_a_new_store_instance_verifies_the_whole_history(tmp_path, verifications):
    with binding() as proof:
        store, tid = await history(tmp_path, proof)
        await store.events(tid)
        reopened = SQLiteSessionStore(store.path, publication=proof)
        verifications.clear()
        assert len(await reopened.events(tid, after=5)) == 3
        assert len(verifications) == 8


async def test_a_cached_prefix_that_disagrees_falls_back_to_full_verification(
    tmp_path, verifications
):
    with binding() as proof:
        store, tid = await history(tmp_path, proof)
        full = await store.events(tid)
        # 模拟进程内记录与数据库不一致（如数据库被恢复成另一份历史）。
        cache = store._verified_prefixes
        cache.record(proof, tid, 0, [VerifiedPrefix("0" * 64, 0)] * (len(full) + 1))
        verifications.clear()
        assert await store.events(tid, after=5) == full[5:]
        assert len(verifications) == 3 + len(full)


def test_cache_is_bound_to_one_publication_binding():
    cache, tid = VerifiedPrefixCache(), uuid4()
    first, second = object(), object()
    cache.record(first, tid, 0, [VerifiedPrefix("0" * 64, 0), VerifiedPrefix("1" * 64, 10)])
    assert cache.prefix(first, tid, 1) == VerifiedPrefix("1" * 64, 10)
    assert cache.prefix(second, tid, 1) is None
    assert cache.prefix(first, tid, 1) is None


def test_cache_keeps_a_bounded_number_of_threads():
    cache, owner = VerifiedPrefixCache(max_threads=2), object()
    threads = [uuid4() for _ in range(3)]
    for tid in threads:
        cache.record(owner, tid, 0, [VerifiedPrefix("0" * 64, 0)])
    assert cache.prefix(owner, threads[0], 0) is None
    assert cache.prefix(owner, threads[2], 0) == VerifiedPrefix("0" * 64, 0)
