"""真实SQLite认证写读契约；显式持久密钥替身不代表默认产品托管完成。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventDraft, ThreadCreated
from harnessix.product_config.contracts import SecretReference
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding

KEY = bytes(range(32))
MATERIAL = "authenticated-store-fixture/+9"


@contextmanager
def binding(store_id, key_id, value=MATERIAL, key=KEY):
    provider = EnvironmentSecretProvider(
        (EnvironmentSecretSource("model", "9", "KEY"),), environment={"KEY": value}
    )
    with SecretPublicationScope((SecretReference(name="model", version="9"),), provider) as scope:
        owned = SessionPublicationBinding(store_id, key_id, key, scope)
        try:
            yield owned
        finally:
            owned.close()


def draft(root, name="workspace"):
    return EventDraft(payload=ThreadCreated(workspace=(root / name).as_posix()))


def ledger(path):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
            for table in (
                "agent_events",
                "agent_threads",
                "agent_event_publications",
                "agent_projection_publications",
            )
        }


async def test_original_event_and_projection_survive_reopen_with_new_scope(tmp_path):
    path, store_id, key_id, thread_id = tmp_path / "state.db", uuid4(), uuid4(), uuid4()
    item = draft(tmp_path)
    with binding(store_id, key_id) as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        original = await store.append(thread_id, [item], expected_sequence=0)
        before = ledger(path)
        assert all(before.values())
    with binding(store_id, key_id, "changed-model-material/+10") as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        assert await store.get_thread(thread_id) == original
        assert len(await store.events(thread_id)) == 1
        assert await store.append(thread_id, [item], expected_sequence=0) == original
        assert ledger(path) == before


@pytest.mark.parametrize("surface", ["get", "list", "recovery", "events", "rebuild"])
async def test_replaced_snapshot_and_unkeyed_hash_do_not_authorize_history(tmp_path, surface):
    path, store_id, key_id, tid = tmp_path / "state.db", uuid4(), uuid4(), uuid4()
    with binding(store_id, key_id) as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        await store.append(tid, [draft(tmp_path)], expected_sequence=0)
        with sqlite3.connect(path) as db:
            raw = db.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
            data = json.loads(raw)
            data["workspace"] = (tmp_path / "forged").as_posix()
            encoded = json.dumps(data, separators=(",", ":"))
            db.execute(
                "UPDATE agent_threads SET snapshot_json=?, snapshot_sha256=?",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest()),
            )
        before = ledger(path)
        if surface in ("get", "list", "recovery"):
            calls = {
                "get": lambda: store.get_thread(tid),
                "list": lambda: store.list_thread_page(after=None, archived=None, limit=10),
                "recovery": store.recovery_threads,
            }
            with pytest.raises(KernelError) as caught:
                await calls[surface]()
            assert caught.value.code == "publication_history_unproven"
            assert ledger(path) == before
        else:
            # 认证事件前缀独立于损坏投影；Rebuild只由原证明恢复，不以当前Scope补签旧正文。
            events = await store.events(tid)
            assert events[0].payload.workspace == (tmp_path / "workspace").as_posix()
            if surface == "rebuild":
                await store.rebuild(tid)
                assert (await store.get_thread(tid)).workspace == events[0].payload.workspace
            else:
                assert ledger(path) == before


@pytest.mark.parametrize("surface", ["events", "rebuild", "duplicate"])
async def test_event_tamper_is_rejected_before_replay_or_duplicate_reapproval(tmp_path, surface):
    path, store_id, key_id, tid = tmp_path / "state.db", uuid4(), uuid4(), uuid4()
    item = draft(tmp_path)
    with binding(store_id, key_id) as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        await store.append(tid, [item], expected_sequence=0)
        with sqlite3.connect(path) as db:
            raw = db.execute("SELECT event_json FROM agent_events").fetchone()[0]
            db.execute("UPDATE agent_events SET event_json=?", (raw + " ",))
        before = ledger(path)
        calls = {
            "events": lambda: store.events(tid),
            "rebuild": lambda: store.rebuild(tid),
            "duplicate": lambda: store.append(tid, [item], expected_sequence=0),
        }
        with pytest.raises(KernelError) as caught:
            await calls[surface]()
        assert caught.value.code == "publication_history_unproven"
        assert ledger(path) == before


async def test_unproven_legacy_database_is_not_bootstrapped_or_rewritten(tmp_path):
    path, tid = tmp_path / "legacy.db", uuid4()
    old = SQLiteSessionStore(path)
    await old.initialize()
    await old.append(tid, [draft(tmp_path, MATERIAL)], expected_sequence=0)
    before = ledger(path)
    with binding(uuid4(), uuid4(), "new-current-material") as proof:
        new = SQLiteSessionStore(path, publication=proof)
        with pytest.raises(KernelError) as caught:
            await new.initialize()
        assert caught.value.code == "publication_history_unproven"
        assert ledger(path) == before


@pytest.mark.parametrize("wrong", ["key", "key_id", "store_id", "none"])
async def test_wrong_binding_and_unprotected_reopen_fail_closed(tmp_path, wrong):
    path, sid, kid, tid = tmp_path / "state.db", uuid4(), uuid4(), uuid4()
    with binding(sid, kid) as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        await store.append(tid, [draft(tmp_path)], expected_sequence=0)
    with binding(
        uuid4() if wrong == "store_id" else sid,
        uuid4() if wrong == "key_id" else kid,
        key=bytes(reversed(KEY)) if wrong == "key" else KEY,
    ) as proof:
        new = SQLiteSessionStore(path, publication=None if wrong == "none" else proof)
        before = ledger(path)
        with pytest.raises(KernelError):
            await new.initialize()
        with pytest.raises(KernelError):
            await new.get_thread(tid)
        assert ledger(path) == before


@pytest.mark.parametrize("point", ["session.after_events", "session.after_projection"])
async def test_atomic_fault_rolls_back_event_seal_and_projection(tmp_path, point):
    def fail(actual):
        if actual == point:
            raise RuntimeError("fixture-fault")

    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof, fault=fail)
        await store.initialize()
        before = ledger(store.path)
        with pytest.raises(RuntimeError):
            await store.append(uuid4(), [draft(tmp_path)], expected_sequence=0)
        assert ledger(store.path) == before


async def test_original_secret_denial_has_no_durable_candidate(tmp_path):
    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        before = ledger(store.path)
        with pytest.raises(KernelError) as caught:
            await store.append(uuid4(), [draft(tmp_path, MATERIAL)], expected_sequence=0)
        assert caught.value.code == "public_output_secret_leak"
        assert ledger(store.path) == before


@pytest.mark.parametrize(
    "table",
    ["agent_event_publications", "agent_projection_publications", "agent_publication_store"],
)
@pytest.mark.parametrize("surface", ["get", "events", "recovery"])
async def test_missing_original_proof_is_never_reissued(tmp_path, table, surface):
    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        tid = uuid4()
        await store.append(tid, [draft(tmp_path)], expected_sequence=0)
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM " + table)
        before = ledger(store.path)
        calls = {
            "get": lambda: store.get_thread(tid),
            "events": lambda: store.events(tid),
            "recovery": store.recovery_threads,
        }
        with pytest.raises(KernelError) as caught:
            await calls[surface]()
        assert caught.value.code == "publication_history_unproven"
        assert ledger(store.path) == before


async def test_cached_commit_response_loss_preserves_original_receipt(tmp_path):
    fired = False

    def fail(point):
        nonlocal fired
        if point == "session.after_commit" and not fired:
            fired = True
            raise RuntimeError("response-lost")

    sid, kid, tid, item = uuid4(), uuid4(), uuid4(), draft(tmp_path)
    path = tmp_path / "state.db"
    with binding(sid, kid) as proof:
        store = SQLiteSessionStore(path, publication=proof, fault=fail)
        await store.initialize()
        with pytest.raises(RuntimeError):
            await store.append(tid, [item], expected_sequence=0)
        before = ledger(path)
    with binding(sid, kid, "new-current-material") as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        assert (await store.append(tid, [item], expected_sequence=0)).sequence == 1
        assert ledger(path) == before


async def test_scope_loss_during_new_body_guard_rolls_back_batch(tmp_path, monkeypatch):
    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        scope = proof._events._protection
        original = scope.assert_public_jsonl

        def closing(body, *, checkpoint):
            original(body, checkpoint=checkpoint)
            scope.close()

        monkeypatch.setattr(scope, "assert_public_jsonl", closing)
        before = ledger(store.path)
        with pytest.raises(KernelError):
            await store.append(uuid4(), [draft(tmp_path)], expected_sequence=0)
        assert ledger(store.path) == before


async def test_parent_cancel_at_body_guard_has_no_committed_proof(tmp_path, monkeypatch):
    import asyncio

    from harnessix.agent import publication

    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        reached = asyncio.Event()

        async def wait(seconds):
            reached.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(publication.asyncio, "sleep", wait)
        before = ledger(store.path)
        task = asyncio.create_task(store.append(uuid4(), [draft(tmp_path)], expected_sequence=0))
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ledger(store.path) == before


async def test_authenticated_runtime_model_history_and_fork_rebuild(tmp_path):
    from harnessix.agent.lifecycle import prepare_fork_snapshot
    from harnessix.agent.models import ThreadForked
    from harnessix.agent.runtime import AgentRuntime
    from harnessix.context.tool_result_contracts import ToolResultViewPolicy
    from harnessix.models.contracts import (
        ResponseCompleted,
        ResponseStarted,
        TextCompleted,
        TextDelta,
        TextStarted,
    )
    from harnessix.models.scripted import ScriptedProvider

    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="r"),
                    TextStarted(content_id="a"),
                    TextDelta(content_id="a", delta="安全答复"),
                    TextCompleted(content_id="a", text="安全答复"),
                    ResponseCompleted(),
                ]
            ]
        )
        async with AgentRuntime(
            store, provider, public_output_protection=proof._events._protection
        ) as runtime:
            parent = await runtime.create_thread(tmp_path.as_posix())
            turn = await runtime.run_turn(parent.thread_id, "安全任务", request_id="auth-runtime")
            assert turn.status.value == "completed" and len(provider.requests) == 1
            source = await store.get_thread(parent.thread_id)
            snapshot = prepare_fork_snapshot(
                source, request_id="fork", through_turn_id=None, policy=ToolResultViewPolicy()
            ).snapshot
            fork = EventDraft(payload=ThreadForked(workspace=source.workspace, snapshot=snapshot))
            child = await store.fork(
                source.thread_id, uuid4(), fork, expected_source_sequence=source.sequence
            )
            assert await store.rebuild(child.thread_id) == child
            assert "安全答复" in (await store.get_thread(child.thread_id)).model_dump_json()


@pytest.mark.parametrize("fault", ["timeout", "bytes", "events"])
async def test_authenticated_replay_resource_failure_is_fixed_and_read_only(
    tmp_path, monkeypatch, fault
):
    from harnessix.session import sqlite_publication

    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        tid = uuid4()
        await store.append(tid, [draft(tmp_path)], expected_sequence=0)
        before = ledger(store.path)
        if fault == "timeout":
            ticks = iter((0, 11))
            monkeypatch.setattr(sqlite_publication, "monotonic", lambda: next(ticks))
        elif fault == "bytes":
            monkeypatch.setattr(sqlite_publication, "MAX_HISTORY_BYTES", 0)
        else:
            monkeypatch.setattr(sqlite_publication, "MAX_HISTORY_EVENTS", 0)
        with pytest.raises(KernelError) as caught:
            await store.events(tid)
        assert caught.value.code == (
            "publication_history_timeout" if fault == "timeout" else "publication_history_limit"
        )
        assert ledger(store.path) == before


@pytest.mark.parametrize(
    "point", ["session.after_events", "session.after_projection", "session.after_commit"]
)
async def test_real_process_exit_settles_original_sqlite_transaction(tmp_path, point):
    import asyncio
    import subprocess
    import sys

    sid, kid, tid = uuid4(), uuid4(), uuid4()
    path = tmp_path / "state.db"
    code = """
import asyncio, os, sys
from pathlib import Path
from uuid import UUID
from harnessix.agent.models import EventDraft, ThreadCreated
from harnessix.product_config.contracts import SecretReference
from harnessix.secrets.provider import EnvironmentSecretProvider,EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.session.sqlite import SQLiteSessionStore
provider=EnvironmentSecretProvider((EnvironmentSecretSource('model','9','KEY'),),environment={'KEY':'child-fixture-material'})
async def main():
 with SecretPublicationScope((SecretReference(name='model',version='9'),),provider) as scope:
  proof=SessionPublicationBinding(UUID(sys.argv[2]),UUID(sys.argv[3]),bytes(range(32)),scope)
  def fault(actual):
   if actual==sys.argv[5]:os._exit(70)
  store=SQLiteSessionStore(sys.argv[1],publication=proof,fault=fault)
  await store.initialize()
  item=EventDraft(payload=ThreadCreated(workspace=Path(sys.argv[1]).parent.as_posix()))
  await store.append(UUID(sys.argv[4]),[item],expected_sequence=0)
asyncio.run(main())
"""
    with binding(sid, kid) as proof:
        store = SQLiteSessionStore(path, publication=proof)
        await store.initialize()
        before = ledger(path)
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code, str(path), str(sid), str(kid), str(tid), point],
            capture_output=True,
            timeout=15,
        )
        assert child.returncode == 70, child.stderr
        if point == "session.after_commit":
            assert (await store.get_thread(tid)).sequence == 1
            assert len(await store.events(tid)) == 1
            assert all(ledger(path).values())
        else:
            assert ledger(path) == before


async def test_cas_with_two_real_authenticated_connections(tmp_path):
    import asyncio

    from harnessix.agent.models import Budget, TurnStarted

    sid, kid, tid, path = uuid4(), uuid4(), uuid4(), tmp_path / "state.db"
    with binding(sid, kid) as first, binding(sid, kid, "other-current-material") as second:
        stores = [SQLiteSessionStore(path, publication=p) for p in (first, second)]
        await asyncio.gather(*(s.initialize() for s in stores))
        await stores[0].append(tid, [draft(tmp_path)], expected_sequence=0)
        results = await asyncio.gather(
            *(
                s.append(
                    tid,
                    [
                        EventDraft(
                            turn_id=uuid4(),
                            payload=TurnStarted(
                                request_id=str(i), request_fingerprint="0" * 64, budget=Budget()
                            ),
                        )
                    ],
                    expected_sequence=1,
                )
                for i, s in enumerate(stores)
            ),
            return_exceptions=True,
        )
        rejected = [r for r in results if isinstance(r, KernelError)]
        assert len(rejected) == 1 and rejected[0].code == "sequence_conflict"
        assert (await stores[0].get_thread(tid)).sequence == 2
        assert len(await stores[1].events(tid)) == 2
        assert len(ledger(path)["agent_event_publications"]) == 2


async def test_truncated_valid_events_do_not_become_a_new_authenticated_prefix(tmp_path):
    from harnessix.agent.models import ThreadArchived

    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        tid = uuid4()
        await store.append(
            tid,
            [draft(tmp_path), EventDraft(payload=ThreadArchived(reason=None))],
            expected_sequence=0,
        )
        with sqlite3.connect(store.path) as db:
            identity = db.execute("SELECT event_id FROM agent_events WHERE sequence=2").fetchone()[
                0
            ]
            db.execute("DELETE FROM agent_event_publications WHERE event_id=?", (identity,))
            db.execute("DELETE FROM agent_events WHERE event_id=?", (identity,))
        before = ledger(store.path)
        with pytest.raises(KernelError) as caught:
            await store.rebuild(tid)
        assert caught.value.code == "publication_history_unproven"
        assert ledger(store.path) == before


@pytest.mark.parametrize("limit", ["MAX_HISTORY_BYTES", "MAX_HISTORY_EVENTS"])
async def test_write_rejects_history_that_cannot_be_replayed(tmp_path, monkeypatch, limit):
    from harnessix.session import sqlite_publication

    with binding(uuid4(), uuid4()) as proof:
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        await store.initialize()
        before = ledger(store.path)
        monkeypatch.setattr(sqlite_publication, limit, 0)
        with pytest.raises(KernelError) as caught:
            await store.append(uuid4(), [draft(tmp_path)], expected_sequence=0)
        assert caught.value.code == "publication_history_limit"
        assert ledger(store.path) == before


@pytest.mark.parametrize("point", [None, "artifact.before_commit"])
async def test_actual_runtime_artifact_transaction_shares_session_proofs(tmp_path, point):
    from harnessix.agent.runtime import AgentRuntime
    from harnessix.artifacts.sqlite import SQLiteArtifactStore
    from harnessix.models.scripted import ScriptedProvider
    from harnessix.tools.runtime import CodingToolRuntime
    from tests.agent.helpers import answer
    from tests.artifacts.helpers import results, step

    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.txt").write_text("needle\n" * 300)

    def fail(actual):
        if actual == point:
            raise RuntimeError("artifact-fault")

    with binding(uuid4(), uuid4()) as proof:
        scope = proof._events._protection
        store = SQLiteSessionStore(tmp_path / "state.db", publication=proof)
        artifacts = SQLiteArtifactStore(store, public_output_protection=scope, fault=fail)
        async with CodingToolRuntime(root, artifacts=artifacts) as tools:
            async with AgentRuntime(
                store,
                ScriptedProvider([step(), answer()]),
                scoped_tools=tools,
                artifacts=artifacts,
                public_output_protection=scope,
            ) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                turn = await runtime.run_turn(
                    thread.thread_id, "归档搜索", request_id="auth-artifact"
                )
        with sqlite3.connect(store.path) as db:
            count = db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]
            events = db.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]
            seals = db.execute("SELECT COUNT(*) FROM agent_event_publications").fetchone()[0]
        assert events == seals and len(await store.events(thread.thread_id)) == events
        if point is None:
            assert turn.status.value == "completed" and count == 1, (
                turn.error.code,
                turn.error.message,
            )
            assert results(turn)[0].output["artifact"]["complete"] is True
        else:
            assert turn.status.value == "failed" and count == 0
