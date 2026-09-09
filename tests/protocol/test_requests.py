from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.protocol.requests import ProtocolRequestError, SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore


async def _ledger(path: Path) -> SQLiteProtocolRequestStore:
    await SQLiteSessionStore(path).initialize()
    return SQLiteProtocolRequestStore(path)


async def test_claim_is_durable_idempotent_and_does_not_store_params(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    ledger = await _ledger(path)
    client = uuid4()
    params = {"requestId": "command-1", "prompt": "不可持久化的提示正文"}

    first = await ledger.claim(client, "command-1", "turn/start", params)
    reopened = SQLiteProtocolRequestStore(path)
    duplicate = await reopened.claim(client, "command-1", "turn/start", params)

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.record.state == "accepted"
    with sqlite3.connect(path) as database:
        row = database.execute("SELECT method, params_sha256 FROM protocol_requests").fetchone()
        assert row is not None and row[0] == "turn/start" and len(row[1]) == 64
        dump = "\n".join(database.iterdump())
    assert "不可持久化的提示正文" not in dump


async def test_same_request_id_with_different_command_is_rejected(tmp_path: Path) -> None:
    ledger = await _ledger(tmp_path / "session.db")
    client = uuid4()
    await ledger.claim(client, "same", "thread/archive", {"threadId": "one"})
    with pytest.raises(ProtocolRequestError) as caught:
        await ledger.claim(client, "same", "thread/archive", {"threadId": "two"})
    assert caught.value.code == "idempotency_conflict"


async def test_terminal_result_survives_reopen_and_cannot_change(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    ledger = await _ledger(path)
    client = uuid4()
    await ledger.claim(client, "done", "thread/create", {"workspace": "/tmp/work"})
    completed = await ledger.complete(client, "done", {"threadId": "stable"})

    reopened = SQLiteProtocolRequestStore(path)
    replayed = await reopened.complete(client, "done", {"threadId": "stable"})
    assert completed.state == replayed.state == "completed"
    assert replayed.outcome == {"threadId": "stable"}
    with pytest.raises(ProtocolRequestError) as caught:
        await reopened.fail(client, "done", {"code": "late_failure"})
    assert caught.value.code == "request_state_conflict"


async def test_corrupt_outcome_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "session.db"
    ledger = await _ledger(path)
    client = uuid4()
    await ledger.claim(client, "done", "thread/create", {})
    await ledger.complete(client, "done", {"threadId": "stable"})
    with sqlite3.connect(path) as database:
        database.execute(
            "UPDATE protocol_requests SET outcome_json='{}' WHERE client_instance_id=?",
            (str(client),),
        )
        database.commit()
    with pytest.raises(ProtocolRequestError) as caught:
        await ledger.get(client, "done")
    assert caught.value.code == "request_corrupt"
