import json
import sqlite3
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, EventDraft, ItemStarted, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.batch_diff import SQLiteBatchDiffPublisher
from harnessix.artifacts.contracts import ArtifactPolicy
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import utc_now
from harnessix.models._history import messages_for
from harnessix.models.scripted import ScriptedProvider
from harnessix.patches.batch_agent_bridge import ManagedPatchBatchBridge
from harnessix.patches.diff_document_contracts import BatchDiffDocumentOptions
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.patches.kernel_batch_helpers import approval_of, batch_step, decide
from tests.patches.test_kernel_patch import REJECT, results
from tests.patches.test_managed_batches import PATHS, snapshot
from tests.patches.test_managed_batches import group_case as group_case


async def exercise(
    case, path, *, policy=None, fault=None, options=None, mode="applied", budget=None
):
    _, _, copy, _, prepared = case
    session = SQLiteSessionStore(path)
    artifacts = SQLiteArtifactStore(session, policy=policy, fault=fault)
    async with ManagedPatchBatchBridge(copy) as bridge:
        provider = ScriptedProvider([batch_step(copy, bridge, prepared), answer()])
        publisher = SQLiteBatchDiffPublisher(artifacts, bridge, options=options)
        async with AgentRuntime(
            session, provider, patch_batches=bridge, batch_diffs=publisher
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(
                thread.thread_id, "报告归档", request_id="diff", budget=budget
            )
            assert waiting.status == TurnStatus.WAITING_APPROVAL
            if mode == "reject":
                await decide(runtime, thread.thread_id, waiting, REJECT)
            else:
                await decide(runtime, thread.thread_id, waiting)
            if mode == "changed":
                (copy.workspace.root / PATHS[1]).write_bytes(b"unrelated")
            completed = await runtime.resume_turn(thread.thread_id, waiting.turn_id)
    return session, artifacts, thread, waiting, completed, provider


@pytest.mark.parametrize("mode", ["applied", "reject", "changed"])
async def test_real_plan_and_effect_are_distinct_atomic_refs(group_case, tmp_path, mode):
    source, _, copy, _, _ = group_case
    original = snapshot(source.root)
    session, artifacts, thread, waiting, completed, provider = await exercise(
        group_case, tmp_path / "s.db", mode=mode
    )
    request, result = approval_of(waiting), results(completed)[0]
    assert request.diff_artifact is not None and result.diff_artifact is not None
    assert request.diff_artifact != result.diff_artifact
    assert approval_of(completed).diff_artifact == request.diff_artifact
    assert result.output["effect"] == ("applied" if mode == "applied" else "not_applied")
    assert result.outcome == ("succeeded" if mode == "applied" else "failed")
    for view, ref in (("plan", request.diff_artifact), ("effect", result.diff_artifact)):
        lines, offset = [], 0
        while True:
            page = await artifacts.read(
                thread.thread_id, copy.workspace.scope, ref.artifact_id, offset=offset, limit=1
            )
            lines.extend(json.loads(line) for line in page.text.splitlines())
            if page.next_offset is None:
                break
            offset = page.next_offset
        assert len(lines) == ref.records and lines[0]["view"] == view
        assert lines[0]["complete"] == ref.complete
        if view == "effect":
            assert lines[0]["effect"] == result.output["effect"]
            assert lines[0]["returned_edits"] == (3 if mode == "applied" else 0)
    with sqlite3.connect(session.path) as db:
        assert db.execute("SELECT purpose FROM agent_artifacts ORDER BY purpose").fetchall() == [
            ("batch_effect",),
            ("batch_plan",),
        ]
        assert db.execute("SELECT COUNT(DISTINCT call_id) FROM agent_artifacts").fetchone()[0] == 1
    stored = await session.get_thread(thread.thread_id)
    assert replay(await session.events(thread.thread_id)) == stored
    assert await session.rebuild(thread.thread_id) == stored
    assert snapshot(source.root) == original
    if len(provider.requests) == 2:
        wire = json.dumps(messages_for(provider.requests[-1]))
        assert str(result.diff_artifact.artifact_id) in wire
        for private in ("patch_batch", "approval_fingerprint", "workspace_id", "batch_id"):
            assert private not in wire


@pytest.mark.parametrize("point", ["after_insert", "before_commit", "after_commit"])
@pytest.mark.parametrize("view", ["plan", "effect"])
async def test_report_fault_preserves_facts_or_acknowledges_commit(
    group_case, tmp_path, point, view
):
    hits = 0

    def fault(name):
        nonlocal hits
        if name == "batch_diff." + point:
            hits += 1
            if hits == (1 if view == "plan" else 2):
                raise RuntimeError("report-only failure")

    session, _, thread, waiting, completed, provider = await exercise(
        group_case, tmp_path / "s.db", fault=fault
    )
    assert completed.status == TurnStatus.COMPLETED
    result = results(completed)[0]
    assert result.outcome == "succeeded" and result.patch_batch.origin == "execution"
    target = approval_of(waiting) if view == "plan" else result
    assert (target.diff_artifact is not None) == (point == "after_commit")
    assert len(results(completed)) == 1 and len(provider.requests) == 2
    assert replay(await session.events(thread.thread_id)) == await session.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize(
    "policy",
    [
        ArtifactPolicy(max_turn_count=1),
        ArtifactPolicy(max_manifests=1),
        ArtifactPolicy(max_turn_bytes=1),
        ArtifactPolicy(max_live_bytes=1),
    ],
)
async def test_quota_does_not_erase_written_effect(group_case, tmp_path, policy):
    _, _, _, waiting, completed, _ = await exercise(group_case, tmp_path / "s.db", policy=policy)
    assert results(completed)[0].diff_artifact is None
    assert results(completed)[0].outcome == "succeeded"
    assert completed.status == TurnStatus.COMPLETED
    assert (approval_of(waiting).diff_artifact is not None) == (
        policy.max_turn_bytes > 1 and policy.max_live_bytes > 1
    )


async def test_report_budget_omission_keeps_original_evidence(group_case, tmp_path):
    _, _, _, waiting, completed, _ = await exercise(
        group_case, tmp_path / "s.db", options=BatchDiffDocumentOptions(max_output_bytes=1024)
    )
    assert approval_of(waiting).diff_artifact is None
    assert results(completed)[0].diff_artifact is None
    assert results(completed)[0].patch_batch.execution.effect == "applied"


@pytest.mark.parametrize("view", ["plan", "effect"])
async def test_reference_scope_corruption_and_ttl(group_case, tmp_path, monkeypatch, view):
    _, _, copy, _, _ = group_case
    session, artifacts, thread, waiting, completed, _ = await exercise(
        group_case, tmp_path / "s.db"
    )
    ref = (approval_of(waiting) if view == "plan" else results(completed)[0]).diff_artifact
    for tid, scope in ((uuid4(), copy.workspace.scope), (thread.thread_id, "0" * 64)):
        with pytest.raises(KernelError) as error:
            await artifacts.read(tid, scope, ref.artifact_id)
        assert error.value.code == "artifact_not_found"
    with sqlite3.connect(session.path) as db:
        row = db.execute(
            "SELECT body FROM agent_artifacts WHERE artifact_id=?", (str(ref.artifact_id),)
        ).fetchone()
        db.execute(
            "UPDATE agent_artifacts SET body=? WHERE artifact_id=?",
            (b" " * len(row[0]), str(ref.artifact_id)),
        )
    with pytest.raises(KernelError) as error:
        await artifacts.read(thread.thread_id, copy.workspace.scope, ref.artifact_id)
    assert error.value.code == "artifact_corrupt"
    with sqlite3.connect(session.path) as db:
        db.execute(
            "UPDATE agent_artifacts SET body=? WHERE artifact_id=?", (row[0], str(ref.artifact_id))
        )
    monkeypatch.setattr("harnessix.artifacts.sqlite.utc_now", lambda: utc_now() + timedelta(days=8))
    with pytest.raises(KernelError) as error:
        await artifacts.read(thread.thread_id, copy.workspace.scope, ref.artifact_id)
    assert error.value.code == "artifact_expired"
    report = await artifacts.collect()
    assert report.expired == 2
    assert (await session.get_thread(thread.thread_id)).turns[-1] == completed


async def test_waiting_plan_is_protected_then_collected(group_case, tmp_path, monkeypatch):
    _, _, copy, _, prepared = group_case
    session = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(session)
    async with ManagedPatchBatchBridge(copy) as bridge:
        async with AgentRuntime(
            session,
            ScriptedProvider([batch_step(copy, bridge, prepared)]),
            patch_batches=bridge,
            batch_diffs=SQLiteBatchDiffPublisher(artifacts, bridge),
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = await runtime.run_turn(thread.thread_id, "等待", request_id="wait")
            monkeypatch.setattr(
                "harnessix.artifacts.sqlite.utc_now", lambda: utc_now() + timedelta(days=8)
            )
            assert (await artifacts.collect()).protected == 1
            await runtime.cancel(thread.thread_id, waiting.turn_id)
            assert (await artifacts.collect()).expired == 1


@pytest.mark.parametrize("schema", list(range(1, 8)))
@pytest.mark.parametrize("view", ["plan", "effect"])
async def test_old_events_cannot_carry_new_refs(group_case, tmp_path, schema, view):
    _, _, _, waiting, completed, _ = await exercise(group_case, tmp_path / "s.db")
    content = approval_of(waiting) if view == "plan" else results(completed)[0]
    with pytest.raises(ValidationError):
        EventDraft(
            schema_version=schema,
            turn_id=waiting.turn_id,
            payload=ItemStarted(item_id=uuid4(), content=content),
        )
    assert EventDraft(payload=ItemStarted(item_id=uuid4(), content=content)).schema_version == 8
    stripped = content.model_copy(update={"diff_artifact": None})
    if schema == 7:
        assert (
            "diff_artifact"
            not in EventDraft(
                schema_version=7, payload=ItemStarted(item_id=uuid4(), content=stripped)
            ).model_dump_json()
        )


async def test_reference_requires_real_batch_evidence(group_case, tmp_path):
    _, _, _, _, completed, _ = await exercise(group_case, tmp_path / "s.db")
    result = results(completed)[0]
    with pytest.raises(ValidationError):
        ToolResultContent(call_id=uuid4(), outcome="succeeded", diff_artifact=result.diff_artifact)


@pytest.mark.parametrize("point", ["before_replace", "after_replace"])
@pytest.mark.parametrize("position", range(3))
async def test_partial_unknown_reports_do_not_invent_edits(
    group_case, tmp_path, monkeypatch, point, position
):
    from harnessix.patches import managed

    count = 0

    def fail(at):
        nonlocal count
        if at == point:
            index, count = count, count + 1
            if index == position:
                raise OSError("成员故障")

    monkeypatch.setattr(managed, "_fault", fail)
    session, artifacts, thread, _, completed, provider = await exercise(
        group_case, tmp_path / "s.db"
    )
    result = results(completed)[0]
    assert result.outcome == ("unknown" if point == "after_replace" else "failed")
    ref = result.diff_artifact
    assert ref is not None and ref.complete
    page = await artifacts.read(thread.thread_id, group_case[2].workspace.scope, ref.artifact_id)
    lines = [json.loads(line) for line in page.text.splitlines()]
    assert lines[0]["effect"] == result.patch_batch.execution.effect
    assert lines[0]["returned_edits"] == position
    assert [r["path"] for r in lines if r["kind"] == "edit"] == list(PATHS[:position])
    assert len(provider.requests) == 1
    assert replay(await session.events(thread.thread_id)) == await session.get_thread(
        thread.thread_id
    )


@pytest.mark.parametrize("view", ["plan", "effect"])
@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
async def test_task_cancel_during_publication_keeps_atomic_truth(
    group_case, tmp_path, monkeypatch, view, point
):
    import asyncio

    _, _, copy, _, prepared = group_case
    session = SQLiteSessionStore(tmp_path / "s.db")
    artifacts = SQLiteArtifactStore(session)
    entered, release = asyncio.Event(), asyncio.Event()
    async with ManagedPatchBatchBridge(copy) as bridge:
        publisher = SQLiteBatchDiffPublisher(artifacts, bridge)
        async with AgentRuntime(
            session,
            ScriptedProvider([batch_step(copy, bridge, prepared), answer()]),
            patch_batches=bridge,
            batch_diffs=publisher,
        ) as runtime:
            thread = await runtime.create_thread(str(copy.workspace.root))
            waiting = None
            if view == "effect":
                waiting = await runtime.run_turn(thread.thread_id, "取消", request_id="cancel")
                await decide(runtime, thread.thread_id, waiting)
            original = (
                session._append_in_transaction if point == "before_commit" else publisher.append
            )

            async def pause(*args, **kwargs):
                result = await original(*args, **kwargs)
                # 仅选中含新报告的批次；正常状态转移直接通过。
                batch = args[2] if point == "before_commit" else args[1]
                if (
                    any(
                        isinstance(d.payload, ItemStarted)
                        and (
                            d.payload.content.kind == "patch_batch_approval_request"
                            if view == "plan"
                            else isinstance(d.payload.content, ToolResultContent)
                            and d.payload.content.patch_batch
                        )
                        for d in batch
                    )
                    and not entered.is_set()
                ):
                    entered.set()
                    await release.wait()
                return result

            monkeypatch.setattr(
                session if point == "before_commit" else publisher,
                "_append_in_transaction" if point == "before_commit" else "append",
                pause,
            )
            task = asyncio.create_task(
                runtime.run_turn(thread.thread_id, "取消", request_id="cancel")
                if waiting is None
                else runtime.resume_turn(thread.thread_id, waiting.turn_id)
            )
            try:
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    saved = await session.get_thread(thread.thread_id)
    final = saved.turns[-1]
    assert replay(await session.events(thread.thread_id)) == saved
    assert len(results(final)) == 1
    if view == "effect":
        assert results(final)[0].patch_batch.execution.effect == "applied"
        assert results(final)[0].diff_artifact is not None
    with sqlite3.connect(session.path) as db:
        for row in db.execute("SELECT artifact_id FROM agent_artifacts"):
            page = await artifacts.read(thread.thread_id, copy.workspace.scope, UUID(row[0]))
            assert page.text


async def test_reference_budget_omission_does_not_truncate_evidence(group_case, tmp_path):
    _, _, _, waiting, completed, _ = await exercise(
        group_case, tmp_path / "s.db", budget=Budget(max_output_chars=1000)
    )
    result = results(completed)[0]
    assert completed.status == TurnStatus.COMPLETED
    assert approval_of(waiting).diff_artifact is not None
    assert result.outcome == "succeeded" and result.diff_artifact is None
    assert result.patch_batch.execution.effect == "applied"
    assert result.output is not None
    assert len(result.model_dump_json(exclude={"patch", "patch_batch"})) <= 1000


@pytest.mark.parametrize("view", ["plan", "effect"])
@pytest.mark.parametrize("field", ["call_id", "workspace_scope", "purpose"])
async def test_reference_cannot_be_rebound(group_case, tmp_path, view, field):
    session, artifacts, thread, waiting, completed, _ = await exercise(
        group_case, tmp_path / "s.db"
    )
    ref = (approval_of(waiting) if view == "plan" else results(completed)[0]).diff_artifact
    scope = group_case[2].workspace.scope
    value = (
        str(uuid4())
        if field == "call_id"
        else "0" * 64
        if field == "workspace_scope"
        else "tool_result"
    )
    with sqlite3.connect(session.path) as db:
        db.execute(
            f"UPDATE agent_artifacts SET {field}=? WHERE artifact_id=?",
            (value, str(ref.artifact_id)),
        )
    with pytest.raises(KernelError) as error:
        await artifacts.read(
            thread.thread_id, value if field == "workspace_scope" else scope, ref.artifact_id
        )
    assert error.value.code == "artifact_corrupt"


@pytest.mark.parametrize("mismatch", ["session", "bridge"])
async def test_publisher_must_match_original_runtime_ports(group_case, tmp_path, mismatch):
    session = SQLiteSessionStore(tmp_path / "s.db")
    async with ManagedPatchBatchBridge(group_case[2]) as bridge:
        publisher = SQLiteBatchDiffPublisher(SQLiteArtifactStore(session), bridge)
        with pytest.raises(KernelError) as error:
            AgentRuntime(
                SQLiteSessionStore(tmp_path / "other.db") if mismatch == "session" else session,
                ScriptedProvider([]),
                patch_batches=bridge if mismatch == "session" else None,
                batch_diffs=publisher,
            )
        assert error.value.code == "artifact_store_mismatch"


async def test_publisher_needs_live_session_owner(group_case, tmp_path):
    session, artifacts, thread, waiting, _, _ = await exercise(group_case, tmp_path / "s.db")
    async with ManagedPatchBatchBridge(group_case[2]) as bridge:
        publisher = SQLiteBatchDiffPublisher(artifacts, bridge)
        with pytest.raises(KernelError) as error:
            await publisher.append(
                thread.thread_id,
                [
                    EventDraft(
                        turn_id=waiting.turn_id,
                        payload=ItemStarted(item_id=uuid4(), content=approval_of(waiting)),
                    )
                ],
                expected_sequence=(await session.get_thread(thread.thread_id)).sequence,
            )
        assert error.value.code == "artifact_runtime_required"
