"""原已认证字节引用的独立回归；摘要声明不是认证或执行令牌。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, Budget, EventDraft, TurnStarted
from harnessix.session.event_body_refs import EventBodyRef
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.session.sqlite_publication import authenticated_events
from tests.session.test_authenticated_history import binding, business_rows, read, seed


@pytest.mark.parametrize("event_count", [1, 2])
async def test_refs_bind_every_original_authenticated_body_without_business_writes(
    tmp_path, event_count
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        if event_count == 2:
            await store.append(
                tid,
                [
                    EventDraft(
                        turn_id=uuid4(),
                        payload=TurnStarted(
                            request_id="body-ref-fixture",
                            request_fingerprint="0" * 64,
                            budget=Budget(),
                        ),
                    )
                ],
                expected_sequence=1,
            )
        before = business_rows(store.path)
        frame = await read(store, tid)
        with closing(sqlite3.connect(store.path)) as db:
            rows = db.execute(
                "SELECT event_id,sequence,event_json FROM agent_events "
                "WHERE thread_id=? ORDER BY sequence",
                (str(tid),),
            ).fetchall()
        assert type(frame.body_refs) is tuple and len(frame.body_refs) == event_count
        for event, body_ref, (event_id, sequence, body) in zip(
            frame.events, frame.body_refs, rows, strict=True
        ):
            assert type(body_ref) is EventBodyRef
            assert body_ref.thread_id == tid == event.thread_id
            assert str(body_ref.event_id) == event_id == str(event.event_id)
            assert body_ref.sequence == sequence == event.sequence
            assert body_ref.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
            assert not any(hasattr(body_ref, name) for name in ("body", "key", "seal", "execute"))
            with pytest.raises(FrozenInstanceError):
                body_ref.sequence = 99
        assert business_rows(store.path) == before


async def test_read_does_not_serialize_agent_event_to_invent_original_body_hash(
    tmp_path, monkeypatch
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)

        def forbidden(*args, **kwargs):
            raise AssertionError("认证读取不能通过事件重编码生成原字节引用")

        monkeypatch.setattr(AgentEvent, "model_dump", forbidden)
        monkeypatch.setattr(AgentEvent, "model_dump_json", forbidden)
        frame = await read(store, tid)
        with closing(sqlite3.connect(store.path)) as db:
            body = db.execute("SELECT event_json FROM agent_events").fetchone()[0]
        assert frame.body_refs[0].body_sha256 == hashlib.sha256(body.encode()).hexdigest()


async def test_ref_collection_does_not_add_sql_or_change_original_checkpoint_schedule(tmp_path):
    """基线由增量前原主仓单事件真实读取采集；只绑定同一窄夹具的顺序计数。"""
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        original = store._connection
        statements, calls = [], []

        @asynccontextmanager
        async def traced(**kwargs):
            async with original(**kwargs) as db:
                await db.set_trace_callback(statements.append)
                yield db

        store._connection = traced
        frame = await read(store, tid, checkpoint=lambda: calls.append(None))
        assert len(frame.body_refs) == 1
        assert len(calls) == 8
        assert len(statements) == 13
        assert sum(statement.startswith("SELECT") for statement in statements) == 12
        assert [s for s in statements if s in ("BEGIN", "ROLLBACK", "COMMIT")] == ["BEGIN"]


async def test_two_argument_history_remains_an_unproven_semantic_aggregate(tmp_path):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        frame = await read(store, tid)
        legacy = AuthenticatedThreadHistory(frame.thread, frame.events)
        assert legacy.body_refs == ()
        assert legacy != frame
        assert not any(hasattr(legacy, name) for name in ("verify", "authorize", "resume"))


@pytest.mark.parametrize("kind", ["subclass", "nonempty", "tuple"])
async def test_private_collector_rejects_custom_or_reused_sink_before_sql(tmp_path, kind):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        before = business_rows(store.path)
        callbacks = []

        class ForeignList(list):
            def append(self, value):
                callbacks.append(value)
                raise AssertionError("不能调用自定义累积回调")

        sink = {"subclass": ForeignList(), "nonempty": [object()], "tuple": ()}[kind]
        async with store._connection(authenticate=False, read_only=True) as db:
            statements = []
            await db.set_trace_callback(statements.append)
            with pytest.raises(KernelError) as caught:
                await authenticated_events(db, proof, tid, 0, _body_refs=sink)
            assert caught.value.code == "publication_history_unproven"
            assert statements == [] and callbacks == []
        assert business_rows(store.path) == before
