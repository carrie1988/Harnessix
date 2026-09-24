"""历史Artifact批量验证与单条路径的语义等价回归。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import ArtifactRef, HistoryReferenceCheck
from harnessix.models.scripted import FakeProvider
from harnessix.tools.runtime import CodingToolRuntime
from tests.artifacts.helpers import exercise, results


def _entry(thread, result, scope, **overrides) -> HistoryReferenceCheck:
    ref = ArtifactRef.model_validate_json(json.dumps(result.output["artifact"]))
    data = {
        "owner_thread_id": thread.thread_id,
        "call_id": result.call_id,
        "reference": ref,
        "purpose": "tool_result",
        "omitted_field": None,
    }
    data.update(overrides)
    return HistoryReferenceCheck(**data)


async def test_batch_verify_accepts_valid_entries_and_empty(tmp_path) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    result = results(turn)[0]
    entry = _entry(thread, result, scope)
    await artifacts.verify_references((), workspace_scope=scope)
    await artifacts.verify_references((entry, entry), workspace_scope=scope)
    await artifacts.verify_reference(
        thread.thread_id,
        result.call_id,
        entry.reference,
        workspace_scope=scope,
        purpose="tool_result",
    )


@pytest.mark.parametrize(
    "kind", ["thread", "call", "scope", "purpose", "manifest", "body", "expired", "missing"]
)
async def test_batch_verify_rejects_same_errors_as_single(tmp_path, monkeypatch, kind) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    result = results(turn)[0]
    overrides = {}
    if kind == "thread":
        overrides["owner_thread_id"] = uuid4()
    elif kind == "call":
        overrides["call_id"] = uuid4()
    elif kind == "purpose":
        overrides["purpose"] = "batch_effect"
    entry = _entry(thread, result, scope, **overrides)
    scope_value = "0" * 64 if kind == "scope" else scope
    if kind == "manifest":
        entry = _entry(
            thread,
            result,
            scope,
            reference=entry.reference.model_copy(update={"sha256": "0" * 64}),
        )
    elif kind == "body":
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE agent_artifacts SET body = zeroblob(size_bytes)")
    elif kind == "expired":
        monkeypatch.setattr(
            artifact_sqlite,
            "utc_now",
            lambda: entry.reference.expires_at + timedelta(seconds=1),
        )
    elif kind == "missing":
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM agent_artifacts")
    with pytest.raises(KernelError) as error:
        await artifacts.verify_references((entry,), workspace_scope=scope_value)
    assert error.value.code == (
        "artifact_corrupt"
        if kind in {"manifest", "body"}
        else "artifact_expired"
        if kind == "expired"
        else "artifact_not_found"
    )


async def test_batch_verify_rejects_unknown_purpose(tmp_path) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    result = results(turn)[0]
    entry = _entry(thread, result, scope)
    entry = HistoryReferenceCheck(
        owner_thread_id=entry.owner_thread_id,
        call_id=entry.call_id,
        reference=entry.reference,
        purpose="unknown_purpose",
        omitted_field=None,
    )
    with pytest.raises(KernelError) as error:
        await artifacts.verify_references((entry,), workspace_scope=scope)
    assert error.value.code == "artifact_invalid"


async def test_batch_verify_first_failure_precedence(tmp_path) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    result = results(turn)[0]
    valid = _entry(thread, result, scope)
    missing = _entry(
        thread,
        result,
        scope,
        reference=valid.reference.model_copy(update={"artifact_id": uuid4()}),
    )
    with pytest.raises(KernelError) as error:
        await artifacts.verify_references((missing, valid), workspace_scope=scope)
    assert error.value.code == "artifact_not_found"
    corrupt = _entry(
        thread,
        result,
        scope,
        reference=valid.reference.model_copy(update={"sha256": "0" * 64}),
    )
    with pytest.raises(KernelError) as error:
        await artifacts.verify_references((valid, corrupt), workspace_scope=scope)
    assert error.value.code == "artifact_corrupt"


async def test_batch_verify_loads_snapshot_once_per_owner(tmp_path, monkeypatch) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)
    result = results(turn)[0]
    entry = _entry(thread, result, scope)
    calls = 0
    original = store._snapshot  # noqa: SLF001

    async def counting(database, thread_id):
        nonlocal calls
        calls += 1
        return await original(database, thread_id)

    monkeypatch.setattr(store, "_snapshot", counting)  # noqa: SLF001
    await artifacts.verify_references((entry, entry), workspace_scope=scope)
    assert calls == 1


async def test_runtime_falls_back_to_single_verifier(tmp_path) -> None:
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=2)

    class SingleOnly:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def session(self):
            return artifacts.session

        async def verify_reference(self, *args, **kwargs):
            self.calls += 1
            return await artifacts.verify_reference(*args, **kwargs)

    verifier = SingleOnly()
    provider = FakeProvider()
    async with CodingToolRuntime(tmp_path / "repo", artifacts=artifacts) as tools:
        async with AgentRuntime(
            store, provider, artifact_verifier=verifier, artifact_access=tools
        ) as runtime:
            follow = await runtime.run_turn(thread.thread_id, "继续", request_id="fallback")
    assert follow.status == TurnStatus.COMPLETED and verifier.calls >= 1
