from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts import sqlite as artifact_sqlite
from harnessix.artifacts.contracts import ArtifactPolicy, ArtifactToolResult
from harnessix.artifacts.sqlite import SQLiteArtifactStore, records
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import exercise, results, step


async def test_archive_beyond_preview_reopen_integrity_and_replay(tmp_path):
    store, artifacts, scope, thread, turn = await exercise(tmp_path)
    assert turn.status == TurnStatus.COMPLETED
    output = results(turn)[0].output
    assert len(output["preview"]["matches"]) == 2 and output["preview"]["truncated"]
    ref = output["artifact"]
    assert ref["records"] == 300 and ref["complete"] and ref["size_bytes"] > 24000
    reopened = SQLiteArtifactStore(SQLiteSessionStore(store.path))
    found, offset = [], 0
    while True:
        page = await reopened.read(
            thread.thread_id, scope, UUID(ref["artifact_id"]), offset=offset, limit=77
        )
        assert len(page.text.encode()) <= 24 * 1024
        found.extend(json.loads(line) for line in page.text.splitlines())
        if page.next_offset is None:
            break
        assert page.next_offset > offset
        offset = page.next_offset
    assert [hit["line"] for hit in found] == list(range(1, 301))
    assert all(hit["text"] == "needle 中文" for hit in found)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
    assert store.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert (
            "needle 中文"
            not in db.execute("SELECT manifest_json FROM agent_artifacts").fetchone()[0]
        )


@pytest.mark.parametrize("kind", ["thread", "workspace", "missing"])
async def test_unknown_and_cross_owner_have_same_failure(tmp_path, kind):
    _, artifacts, scope, thread, turn = await exercise(tmp_path, count=1)
    ref = UUID(results(turn)[0].output["artifact"]["artifact_id"])
    with pytest.raises(KernelError) as error:
        await artifacts.read(
            uuid4() if kind == "thread" else thread.thread_id,
            "0" * 64 if kind == "workspace" else scope,
            uuid4() if kind == "missing" else ref,
        )
    assert error.value.code == "artifact_not_found"


@pytest.mark.parametrize("field", ["max_turn_bytes", "max_live_bytes"])
async def test_byte_quota_failure_rolls_back_result_and_content(tmp_path, field):
    store, _, _, thread, turn = await exercise(
        tmp_path, policy=ArtifactPolicy(**{field: 1}), count=1
    )
    assert turn.status == TurnStatus.FAILED and turn.error.code == "artifact_quota_exceeded"
    assert not any(r.outcome == "succeeded" for r in results(turn))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("field", ["max_turn_count", "max_manifests"])
async def test_count_quotas_preserve_first_result(tmp_path, field):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store, policy=ArtifactPolicy(**{field: 1}))
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store,
            ScriptedProvider([step(), step(), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "配额", request_id="count")
    assert turn.error.code == "artifact_quota_exceeded"
    assert sum(r.outcome == "succeeded" for r in results(turn)) == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1


async def test_concurrent_publish_cannot_overdraw_global_quota(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x").write_text("needle")
    store = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(store, policy=ArtifactPolicy(max_manifests=1))
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            store, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            threads = [await runtime.create_thread(str(tools.workspace_root)) for _ in range(2)]
            turns = await asyncio.gather(
                *(runtime.run_turn(t.thread_id, "并发", request_id="a") for t in threads)
            )
    assert sorted(t.status.value for t in turns) == ["completed", "failed"]
    assert next(t for t in turns if t.error).error.code == "artifact_quota_exceeded"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["body", "manifest", "record_count", "missing_reference"])
async def test_tampering_is_corruption_not_empty_success(tmp_path, kind):
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=1)
    ref = UUID(results(turn)[0].output["artifact"]["artifact_id"])
    with sqlite3.connect(store.path) as db:
        if kind == "body":
            data = db.execute("SELECT body FROM agent_artifacts").fetchone()[0]
            db.execute("UPDATE agent_artifacts SET body = ?", (b"x" + data[1:],))
        elif kind == "missing_reference":
            db.execute("UPDATE agent_artifacts SET call_id = ?", (str(uuid4()),))
        else:
            manifest = json.loads(
                db.execute("SELECT manifest_json FROM agent_artifacts").fetchone()[0]
            )
            manifest["artifact_id" if kind == "manifest" else "records"] = (
                str(uuid4()) if kind == "manifest" else 99
            )
            db.execute("UPDATE agent_artifacts SET manifest_json = ?", (json.dumps(manifest),))
    with pytest.raises(KernelError) as error:
        await artifacts.read(thread.thread_id, scope, ref)
    assert error.value.code == "artifact_corrupt"


async def test_expiry_cleanup_tombstone_and_empty_content(tmp_path, monkeypatch):
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=0)
    ref = UUID(results(turn)[0].output["artifact"]["artifact_id"])
    page = await artifacts.read(thread.thread_id, scope, ref)
    assert page.artifact.records == 0 and page.text == "" and page.next_offset is None
    future = artifact_sqlite.utc_now() + timedelta(days=2)
    monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)
    with pytest.raises(KernelError) as error:
        await artifacts.read(thread.thread_id, scope, ref)
    assert error.value.code == "artifact_expired"
    report = await artifacts.collect()
    assert report.examined == report.expired == 1 and report.protected == 0
    assert (await artifacts.collect()).examined == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT state, body FROM agent_artifacts").fetchone() == ("expired", None)
    with pytest.raises(KernelError) as error:
        await artifacts.read(thread.thread_id, scope, ref)
    assert error.value.code == "artifact_expired"


@pytest.mark.parametrize("offset,limit", [(True, 1), (0, True), (-1, 2), (0, 201), (301, 1)])
async def test_invalid_cursors_fail(tmp_path, offset, limit):
    _, artifacts, scope, thread, turn = await exercise(tmp_path)
    ref = UUID(results(turn)[0].output["artifact"]["artifact_id"])
    with pytest.raises(KernelError) as error:
        await artifacts.read(thread.thread_id, scope, ref, offset=offset, limit=limit)
    assert error.value.code == "artifact_invalid_cursor"


@pytest.mark.parametrize(
    "body",
    [
        b"unterminated",
        b"\xff\n",
        b"NaN\n",
        b"\n",
        b"{}\n" * 10001,
        b"x" * (1024 * 1024 + 1),
        b'"' + b"a" * 24576 + b'"\n',
    ],
)
def test_invalid_or_unbounded_records_are_rejected(body):
    with pytest.raises(KernelError) as error:
        records(body)
    assert error.value.code == "artifact_invalid"


@pytest.mark.parametrize("point", ["artifact.after_insert", "artifact.before_commit"])
async def test_exception_before_commit_rolls_back_everything(tmp_path, point):
    def fault(name):
        if name == point:
            raise RuntimeError("PRIVATE insertion failed")

    store, _, _, thread, turn = await exercise(tmp_path, fault=fault, count=1)
    assert turn.status == TurnStatus.FAILED and "PRIVATE" not in turn.model_dump_json()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_stale_output_does_not_authorize_publication(tmp_path):
    store, artifacts, scope, thread, turn = await exercise(tmp_path, count=1)
    call = next(i.content for i in turn.items if isinstance(i.content, ToolCallContent))
    product = ArtifactToolResult(results(turn)[0], b"{}\n", scope, True, artifacts)
    with pytest.raises(KernelError) as error:
        await artifacts.publish(
            thread.thread_id,
            turn.turn_id,
            call,
            product,
            expected_sequence=999,
            max_output_chars=65536,
        )
    assert error.value.code == "artifact_runtime_required"
    async with AgentRuntime(store, FakeProvider()):
        snapshot = await store.get_thread(thread.thread_id)
        with pytest.raises(KernelError) as error:
            await artifacts.publish(
                thread.thread_id,
                turn.turn_id,
                call,
                product,
                expected_sequence=snapshot.sequence,
                max_output_chars=65536,
            )
        assert error.value.code == "tool_scope_mismatch"
        with pytest.raises(KernelError) as error:
            await artifacts.publish(
                thread.thread_id,
                turn.turn_id,
                call,
                replace(product, publisher=SQLiteArtifactStore(store)),
                expected_sequence=snapshot.sequence,
                max_output_chars=65536,
            )
        assert error.value.code == "artifact_store_mismatch"


async def test_storage_full_during_publish_is_atomic_and_sanitized(tmp_path):
    def fault(name):
        if name == "artifact.after_insert":
            error = sqlite3.OperationalError("PRIVATE disk detail")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error

    store, _, _, thread, turn = await exercise(tmp_path, fault=fault, count=1)
    assert turn.status == TurnStatus.FAILED and turn.error.code == "storage_full"
    assert "PRIVATE" not in turn.model_dump_json()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_after_commit_error_preserves_reference_and_payload(tmp_path):
    def fault(name):
        if name == "artifact.after_commit":
            raise RuntimeError("PRIVATE commit acknowledgement")

    store, artifacts, scope, thread, turn = await exercise(tmp_path, fault=fault, count=1)
    assert turn.status == TurnStatus.FAILED and "PRIVATE" not in turn.model_dump_json()
    output = results(turn)[0]
    assert output.outcome == "succeeded"
    page = await artifacts.read(
        thread.thread_id, scope, UUID(output.output["artifact"]["artifact_id"])
    )
    assert page.artifact.records == 1
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


async def test_gc_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    store, artifacts, _, _, _ = await exercise(tmp_path, count=1)
    future = artifact_sqlite.utc_now() + timedelta(days=2)
    monkeypatch.setattr(artifact_sqlite, "utc_now", lambda: future)

    def fault(name):
        if name == "artifact.before_collect_commit":
            raise RuntimeError("清理提交失败")

    broken = SQLiteArtifactStore(store, fault=fault)
    with pytest.raises(RuntimeError):
        await broken.collect()
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT state, body FROM agent_artifacts").fetchone()
        assert row[0] == "published" and row[1]
    assert (await artifacts.collect()).expired == 1
    assert (await artifacts.collect()).examined == 0
