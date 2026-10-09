"""同次SQLite原字节复用；不减少认证、解析或跨读取复用结论。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentEvent, EventDraft, Thread, ThreadArchived
from harnessix.session import publication_seal as event_publication_module
from harnessix.session import sqlite as sqlite_module
from harnessix.session import sqlite_publication as publication_module
from harnessix.session import store_publication as store_publication_module
from harnessix.session.store_publication import EMPTY_PREFIX
from tests.session.test_authenticated_history import binding, business_rows, read, seed


def noncanonical_json(encoded):
    return json.dumps(json.loads(encoded), ensure_ascii=False, sort_keys=True) + " "


@pytest.mark.parametrize("noncanonical", [False, True])
@pytest.mark.parametrize("surface", ["history", "get", "recovery"])
async def test_same_row_encodes_once_but_keeps_both_snapshot_hashes_and_parses(
    tmp_path, monkeypatch, noncanonical, surface
):
    with binding() as proof:
        # 非规范正文在新事件签发时产生，读取端不能重编码或补签旧材料。
        with monkeypatch.context() as writer_patch:
            if noncanonical:
                event_json, thread_json = AgentEvent.model_dump_json, Thread.model_dump_json
                writer_patch.setattr(
                    AgentEvent,
                    "model_dump_json",
                    lambda self, *a, **kw: noncanonical_json(event_json(self, *a, **kw)),
                )
                writer_patch.setattr(
                    Thread,
                    "model_dump_json",
                    lambda self, *a, **kw: noncanonical_json(thread_json(self, *a, **kw)),
                )
            store, tid, expected = await seed(tmp_path / "中文🦉" / "state.db", proof)
        with closing(sqlite3.connect(store.path)) as db:
            event_text = db.execute("SELECT event_json FROM agent_events").fetchone()[0]
            snapshot_text = db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
        assert "中文🦉" in event_text and "中文🦉" in snapshot_text
        if noncanonical:
            assert event_text != AgentEvent.model_validate_json(event_text).model_dump_json()
            assert snapshot_text != expected.model_dump_json()
        event_body, snapshot_body = event_text.encode(), snapshot_text.encode()
        assert len(event_body) > len(event_text) and len(snapshot_body) > len(snapshot_text)
        before = business_rows(store.path)
        encodes = {event_text: 0, snapshot_text: 0}
        hashes, parses, verified_bodies, callbacks, statements, order = [], [], [], [], [], []
        macs = []
        original_parse, original_verify = Thread.model_validate_json, proof.verify_event
        original_connection = store._connection

        def profile(_frame, kind, call):
            if kind == "c_call" and getattr(call, "__name__", None) == "encode":
                text = getattr(call, "__self__", None)
                if type(text) is str and text in encodes:
                    encodes[text] += 1

        def digest(stage, body):
            hashes.append((stage, body))
            if body == snapshot_body:
                order.append(stage)
            return hashlib.sha256(body)

        def mac(key, claims, algorithm):
            macs.append(claims)
            return hmac.digest(key, claims, algorithm)

        def parse(encoded, *args, **kwargs):
            result = original_parse(encoded, *args, **kwargs)
            parses.append((encoded, result))
            order.append("parse")
            return result

        def verify(seal, body, *args):
            verified_bodies.append(body)
            return original_verify(seal, body, *args)

        def checkpoint():
            callbacks.append((asyncio.current_task(), threading.get_ident()))

        @asynccontextmanager
        async def connection(**kwargs):
            async with original_connection(**kwargs) as database:
                await database.set_trace_callback(statements.append)
                yield database

        monkeypatch.setattr(
            publication_module, "hashlib", SimpleNamespace(sha256=lambda b: digest("auth", b))
        )
        monkeypatch.setattr(
            sqlite_module, "hashlib", SimpleNamespace(sha256=lambda b: digest("storage", b))
        )
        monkeypatch.setattr(
            event_publication_module,
            "hashlib",
            SimpleNamespace(sha256=lambda b: digest("event", b)),
        )
        for module in (store_publication_module, event_publication_module):
            monkeypatch.setattr(
                module, "hmac", SimpleNamespace(digest=mac, compare_digest=hmac.compare_digest)
            )
        monkeypatch.setattr(Thread, "model_validate_json", parse)
        monkeypatch.setattr(proof, "verify_event", verify)
        monkeypatch.setattr(store, "_connection", connection)
        previous_profile = sys.getprofile()
        sys.setprofile(profile)
        try:
            if surface == "history":
                frame = await read(store, tid, checkpoint=checkpoint)
                assert frame.thread == expected
            elif surface == "get":
                assert await store.get_thread(tid) == expected
            else:
                assert await store.recovery_threads() == ()
        finally:
            sys.setprofile(previous_profile)
        assert encodes == {event_text: int(surface == "history"), snapshot_text: 1}
        assert order == ["auth", "parse", "storage", "parse"]
        assert [text for text, _ in parses] == [snapshot_text, snapshot_text]
        assert parses[0][1] is not parses[1][1]
        assert hashes[0][1] is hashes[1][1]
        assert hashes[0][1] == snapshot_body
        assert len(macs) == (5 if surface == "history" else 3)
        assert len(hashes) == (4 if surface == "history" else 2)
        if surface == "history":
            assert len(verified_bodies) == 1 and hashes[2][1] is verified_bodies[0]
            assert hashes[3][1] is verified_bodies[0]
            assert verified_bodies[0] == event_body
            assert frame.body_refs[0].body_sha256 == hashlib.sha256(event_body).hexdigest()
            assert len(callbacks) == 8
            assert callbacks == [(asyncio.current_task(), threading.get_ident())] * 8
            assert len(statements) == 13
            assert sum(s.startswith("SELECT") for s in statements) == 12
            assert [s for s in statements if s in ("BEGIN", "ROLLBACK", "COMMIT")] == ["BEGIN"]
        assert business_rows(store.path) == before


async def test_snapshot_none_and_legacy_four_argument_validation_remain_compatible(tmp_path):
    store, tid, expected = await seed(tmp_path / "legacy.db", None)
    async with store._connection() as db:
        cursor = await db.execute("SELECT * FROM agent_threads")
        row = await cursor.fetchone()
        assert await publication_module.verify_snapshot(db, None, tid, row) is None
        assert sqlite_module._validated_snapshot(tid, row, 1, 1) == expected
        assert sqlite_module._validated_snapshot(tid, row, 1, 1, snapshot_bytes=None) == expected
        with pytest.raises(TypeError):
            sqlite_module._validated_snapshot(tid, row, 1, 1, row["snapshot_json"].encode())
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "authenticated.db", proof)
        async with store._connection() as db:
            assert await publication_module.verify_snapshot(db, proof, uuid4(), None) is None
            with pytest.raises(KernelError) as caught:
                await publication_module.verify_snapshot(db, proof, tid, None)
            assert caught.value.code == "publication_history_unproven"


@pytest.mark.parametrize("supplied_bytes", [False, True])
@pytest.mark.parametrize(
    "fault,code,message",
    [
        ("gap", "event_corrupt", "序号缺口"),
        ("version", "projection_too_new", "版本"),
        ("sha", "projection_corrupt", "快照校验失败"),
        ("parse", "projection_corrupt", "快照结构损坏"),
        ("sequence", "projection_corrupt", "投影序号"),
    ],
)
async def test_second_validator_preserves_error_order_with_or_without_original_bytes(
    tmp_path, supplied_bytes, fault, code, message
):
    store, tid, _ = await seed(tmp_path / "legacy.db", None)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.row_factory = sqlite3.Row
        body = db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
        if fault != "sequence":
            body = "{}"
        digest = (
            hashlib.sha256(body.encode()).hexdigest()
            if fault in ("parse", "sequence")
            else EMPTY_PREFIX
        )
        db.execute(
            "UPDATE agent_threads SET snapshot_json=?, snapshot_sha256=?, "
            "projection_version=?, sequence=2",
            (body, digest, 22 if fault in ("gap", "version") else 21),
        )
        row = db.execute("SELECT * FROM agent_threads").fetchone()
    kwargs = {"snapshot_bytes": body.encode()} if supplied_bytes else {}
    with pytest.raises(KernelError, match=message) as caught:
        sqlite_module._validated_snapshot(tid, row, 2 if fault == "gap" else 1, 1, **kwargs)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "fault,parses,code",
    [
        ("sha", 0, "publication_history_unproven"),
        ("mac", 0, "publication_history_unproven"),
        ("sequence", 0, "publication_history_unproven"),
        ("prefix", 1, "publication_history_unproven"),
        ("history_size", 1, "publication_history_unproven"),
        ("history_limit", 1, "publication_history_limit"),
        ("event_count", 0, "publication_history_limit"),
    ],
)
async def test_event_authentication_and_capacity_keep_original_failure_order(
    tmp_path, monkeypatch, fault, parses, code
):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "state.db", proof)
        with closing(sqlite3.connect(store.path)) as db, db:
            raw = db.execute("SELECT seal FROM agent_projection_publications").fetchone()[0]
            checkpoint = proof.verify_projection(raw, tid)
            if fault in ("sha", "mac", "sequence", "event_count"):
                db.execute("UPDATE agent_events SET event_json='not-json'")
            if fault == "mac":
                seal = json.loads(
                    db.execute("SELECT seal FROM agent_event_publications").fetchone()[0]
                )
                seal["tag"] = EMPTY_PREFIX
                db.execute(
                    "UPDATE agent_event_publications SET seal=?", (json.dumps(seal).encode(),)
                )
            if fault in ("sequence", "event_count"):
                db.execute("UPDATE agent_events SET sequence=2")
            if fault in ("prefix", "history_size", "history_limit"):
                snapshot = db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
                seal = proof.projection(
                    expected,
                    snapshot,
                    EMPTY_PREFIX if fault != "history_size" else checkpoint.prefix_sha256,
                    checkpoint.history_bytes + int(fault == "history_size"),
                )
                db.execute("UPDATE agent_projection_publications SET seal=?", (seal,))
        if fault in ("sha", "mac", "sequence"):
            monkeypatch.setattr(publication_module, "MAX_HISTORY_BYTES", 0)
        elif fault == "history_limit":
            monkeypatch.setattr(
                publication_module, "MAX_HISTORY_BYTES", checkpoint.history_bytes - 1
            )
        elif fault == "event_count":
            monkeypatch.setattr(publication_module, "MAX_HISTORY_EVENTS", 0)
        before, parsed = business_rows(store.path), []
        original, original_verify = AgentEvent.model_validate_json, proof.verify_event
        verified, event_hashes = [], []

        def parse(body, *args, **kwargs):
            parsed.append(body)
            return original(body, *args, **kwargs)

        def verify(seal, body, *args):
            verified.append(body)
            return original_verify(seal, body, *args)

        def digest(body):
            event_hashes.append(body)
            return hashlib.sha256(body)

        monkeypatch.setattr(AgentEvent, "model_validate_json", parse)
        monkeypatch.setattr(proof, "verify_event", verify)
        monkeypatch.setattr(event_publication_module, "hashlib", SimpleNamespace(sha256=digest))
        with pytest.raises(KernelError) as caught:
            await store.events(tid)
        assert caught.value.code == code and len(parsed) == parses
        assert len(verified) == int(fault not in ("sequence", "event_count"))
        assert len(event_hashes) == int(fault not in ("mac", "sequence", "event_count"))
        assert business_rows(store.path) == before


@pytest.mark.parametrize(
    "fault", ["sha", "mac", "sequence", "count", "prefix", "projection_bytes", "local_sha"]
)
async def test_snapshot_returns_bytes_only_after_all_original_authentication_checks(
    tmp_path, monkeypatch, fault
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        with closing(sqlite3.connect(store.path)) as db, db:
            if fault == "sha":
                db.execute("UPDATE agent_threads SET snapshot_json='{}'")
            elif fault == "mac":
                seal = json.loads(
                    db.execute("SELECT seal FROM agent_projection_publications").fetchone()[0]
                )
                seal["tag"] = EMPTY_PREFIX
                db.execute(
                    "UPDATE agent_projection_publications SET seal=?", (json.dumps(seal).encode(),)
                )
                db.execute("UPDATE agent_threads SET snapshot_json='{}'")
            elif fault == "sequence":
                db.execute("UPDATE agent_threads SET sequence=2, snapshot_json='{}'")
            elif fault == "count":
                db.execute("DELETE FROM agent_event_publications")
            elif fault == "prefix":
                db.execute("UPDATE agent_event_publications SET prefix_sha256=?", (EMPTY_PREFIX,))
            elif fault == "projection_bytes":
                body = db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
                monkeypatch.setattr(publication_module, "MAX_PROJECTION_BYTES", len(body) - 1)
            else:
                db.execute("UPDATE agent_threads SET snapshot_sha256=?", (EMPTY_PREFIX,))
        before, parsed = business_rows(store.path), []
        original_parse = Thread.model_validate_json
        original_validate = sqlite_module._validated_snapshot
        validated = []

        def parse(body, *args, **kwargs):
            parsed.append(body)
            return original_parse(body, *args, **kwargs)

        def validate(*args, **kwargs):
            validated.append(kwargs["snapshot_bytes"])
            return original_validate(*args, **kwargs)

        monkeypatch.setattr(Thread, "model_validate_json", parse)
        monkeypatch.setattr(sqlite_module, "_validated_snapshot", validate)
        with pytest.raises(KernelError) as caught:
            await store.get_thread(tid)
        assert caught.value.code == (
            "projection_corrupt" if fault == "local_sha" else "publication_history_unproven"
        )
        assert len(parsed) == int(fault in ("count", "prefix", "local_sha"))
        assert len(validated) == int(fault == "local_sha")
        assert business_rows(store.path) == before


async def test_next_read_reauthenticates_changed_terminal_thread_and_event_bytes(tmp_path):
    with binding() as proof:
        store, tid, expected = await seed(tmp_path / "state.db", proof)
        first = await read(store, tid)
        terminal = await store.append(
            tid, [EventDraft(payload=ThreadArchived(reason="终态🦉"))], expected_sequence=1
        )
        second = await read(store, tid)
        assert first.thread == expected and second.thread == terminal
        assert second.thread is not first.thread and len(second.events) == 2
        assert second.events[0] is not first.events[0]
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("UPDATE agent_events SET event_json=event_json || ' ' WHERE sequence=2")
        before = business_rows(store.path)
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == "publication_history_unproven"
        assert business_rows(store.path) == before


async def test_closed_binding_cannot_reuse_a_previous_authenticated_read(tmp_path):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        await read(store, tid)
        before = business_rows(store.path)
        proof.close()
        with pytest.raises(KernelError) as caught:
            await read(store, tid)
        assert caught.value.code == "publication_key_unavailable"
        assert business_rows(store.path) == before


async def test_original_ten_second_child_deadline_and_callback_order_are_unchanged(
    tmp_path, monkeypatch
):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "state.db", proof)
        ticks, order, callbacks = iter((100.0, 109.99, 110.0)), [], []

        def now():
            order.append("clock")
            return next(ticks)

        def checkpoint():
            order.append("checkpoint")
            callbacks.append((asyncio.current_task(), threading.get_ident()))

        monkeypatch.setattr(publication_module, "monotonic", now)
        before = business_rows(store.path)
        async with store._connection(read_only=True) as db:
            await db.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                await publication_module.authenticated_events(
                    db, proof, tid, 0, history_checkpoint=checkpoint
                )
        assert caught.value.code == "publication_history_timeout"
        assert order == ["clock", "checkpoint", "clock", "checkpoint", "clock"]
        assert callbacks == [(asyncio.current_task(), threading.get_ident())] * 2
        assert business_rows(store.path) == before
