"""原v20认证、新v21声明及拒绝批次原子性；仅使用离线固定材料。"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing, contextmanager
from importlib.resources import files
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.lifecycle import prepare_fork_snapshot
from harnessix.agent.models import (
    AgentEvent,
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ThreadCreated,
    ThreadForked,
    ThreadForkSnapshotV2,
    ToolCallRejectionContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.reducer import replay
from harnessix.agent.tool_rejections import rejection_result
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.product_config.state_backup_validation import _SESSION_MIGRATION_CHECKSUMS
from harnessix.session import sqlite as sqlite_module
from harnessix.session.publication_seal import EventPublicationSeal
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import (
    EMPTY_PREFIX,
    ProjectionPublicationSeal,
    require_projection_content_version,
)
from tests.session.test_authenticated_history import KEY, binding, business_rows, read, seed

EVENT_DOMAIN = b"harnessix.session-event-seal/v1\x00"
STORE_DOMAIN = b"harnessix.session-store-publication/v1\x00"


@contextmanager
def raises_code(code):
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == code


def fixture_seal(claims, domain, key=KEY):
    """独立冻结原MAC编码，不调用待测签发器来证明自身兼容。"""
    value = dict(claims)
    value.pop("tag", None)
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    value["tag"] = hmac.digest(key, domain + canonical, "sha256").hex()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def fixture_event_seal(proof, event, body, version):
    return fixture_seal(
        {
            "seal_version": 1,
            "policy": "harnessix.public-output-protection/v1",
            "key_id": str(proof._key_id),
            "store_id": str(proof._store_id),
            "thread_id": str(event.thread_id),
            "event_id": str(event.event_id),
            "sequence": event.sequence,
            "event_schema_version": version,
            "scope_sha256": proof._events._context_sha256,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        },
        EVENT_DOMAIN,
        proof._key,
    )


async def original_v20_store(path, proof, workspace=None):
    """植入原完整v30与原v20正文/Seal字节；新写端不拥有旧事件签发权。"""
    store = SQLiteSessionStore(path, publication=proof)
    await store.initialize()
    event = AgentEvent(
        schema_version=20,
        thread_id=uuid4(),
        sequence=1,
        payload=ThreadCreated(workspace=(workspace or path.parent / "workspace").as_posix()),
    )
    body = event.model_dump_json().encode()
    event_seal = fixture_event_seal(proof, event, body, 20)
    prefix = hashlib.sha256(
        bytes.fromhex(EMPTY_PREFIX) + hashlib.sha256(event_seal).digest()
    ).hexdigest()
    thread = replay([event])
    snapshot = thread.model_dump_json()
    projection = fixture_seal(
        {
            "version": 1,
            "purpose": "derived_projection",
            "store_id": str(proof._store_id),
            "key_id": str(proof._key_id),
            "thread_id": str(event.thread_id),
            "sequence": 1,
            "history_bytes": len(body) + len(event_seal),
            "projection_version": 20,
            "prefix_sha256": prefix,
            "snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
        },
        STORE_DOMAIN,
        proof._key,
    )
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("DELETE FROM agent_migrations WHERE version=31")
        database.execute(
            "INSERT INTO agent_events VALUES (?,?,?,?)",
            (str(event.thread_id), 1, str(event.event_id), body.decode()),
        )
        database.execute(
            "INSERT INTO agent_event_publications VALUES (?,?,?)",
            (str(event.event_id), prefix, event_seal),
        )
        database.execute(
            "INSERT INTO agent_threads VALUES (?,?,?,?,?)",
            (str(event.thread_id), 1, snapshot, hashlib.sha256(snapshot.encode()).hexdigest(), 20),
        )
        database.execute(
            "INSERT INTO agent_projection_publications VALUES (?,?)",
            (str(event.thread_id), projection),
        )
    return store, event.thread_id, thread


def projection_rows(path, thread_id=None):
    with closing(sqlite3.connect(path)) as database:
        return database.execute(
            "SELECT projection_version,seal FROM agent_threads "
            "JOIN agent_projection_publications USING(thread_id)"
            + (" WHERE thread_id=?" if thread_id is not None else ""),
            (str(thread_id),) if thread_id is not None else (),
        ).fetchone()


async def calling_model(store, thread_id, sequence):
    turn_id, input_id = uuid4(), uuid4()
    content = TextContent(kind="user_message", text="离线拒绝验证")
    thread = await store.append(
        thread_id,
        [
            EventDraft(
                turn_id=turn_id,
                payload=TurnStarted(
                    request_id="rejection-fixture", request_fingerprint="0" * 64, budget=Budget()
                ),
            ),
            EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=input_id, content=content)),
            EventDraft(
                turn_id=turn_id,
                payload=ItemFinished(
                    item_id=input_id, status=ItemStatus.COMPLETED, content=content
                ),
            ),
            EventDraft(
                turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT)
            ),
            EventDraft(turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.CALLING_MODEL)),
        ],
        expected_sequence=sequence,
    )
    return turn_id, thread.sequence


def rejection_batch(turn_id, count=2):
    calls, results = [], []
    for index in range(count):
        rejected = ToolCallRejectionContent(
            call_id=uuid4(), provider_call_id=f"rejected-{index}", model_step=1
        )
        for target, content in ((calls, rejected), (results, rejection_result(rejected.call_id))):
            item_id = uuid4()
            target.extend(
                [
                    EventDraft(
                        turn_id=turn_id, payload=ItemStarted(item_id=item_id, content=content)
                    ),
                    EventDraft(
                        turn_id=turn_id,
                        payload=ItemFinished(
                            item_id=item_id, status=ItemStatus.COMPLETED, content=content
                        ),
                    ),
                ]
            )
    return tuple(calls + results)


async def test_original_v20_authentication_reads_and_upgrades_without_resigning(tmp_path):
    with binding() as proof:
        store, tid, original = await original_v20_store(tmp_path / "old.db", proof)
        before = business_rows(store.path)
        frame = await read(store, tid)
        assert frame.thread == original and frame.events[0].schema_version == 20
        assert business_rows(store.path) == before
        await store.initialize()
        upgraded = business_rows(store.path)
        assert upgraded["agent_migrations"][-1][0] == 31
        for name in before.keys() - {"agent_migrations"}:
            assert upgraded[name] == before[name]
        await calling_model(store, tid, 1)
        after = business_rows(store.path)
        assert after["agent_events"][0] == before["agent_events"][0]
        assert before["agent_event_publications"][0] in after["agent_event_publications"]
        events = await store.events(tid)
        assert events[0].schema_version == 20
        assert all(event.schema_version == 21 for event in events[1:])
        version, seal = projection_rows(store.path)
        assert (
            version == ProjectionPublicationSeal.model_validate_json(seal).projection_version == 21
        )
        assert (await read(store, tid)).thread == replay(events)


@pytest.mark.parametrize("body_version", [20, 21])
@pytest.mark.parametrize("claim_version", [19, 20, 21, 22])
def test_authenticated_event_version_must_match_original_body(
    body_version, claim_version, tmp_path
):
    with binding() as proof:
        event = AgentEvent(
            schema_version=body_version,
            thread_id=uuid4(),
            sequence=1,
            payload=ThreadCreated(workspace=tmp_path.as_posix()),
        )
        body = event.model_dump_json().encode()
        seal = fixture_event_seal(proof, event, body, claim_version)
        if body_version == claim_version:
            proof.verify_event(seal, body, event.event_id, event.thread_id, event.sequence)
        else:
            with raises_code("publication_history_unproven"):
                proof.verify_event(seal, body, event.event_id, event.thread_id, event.sequence)


async def test_original_v20_seal_defaults_remain_verifiable_without_reencoding_history(tmp_path):
    with binding() as proof:
        store, tid, _ = await original_v20_store(tmp_path / "defaults.db", proof)
        before = business_rows(store.path)
        event_row = before["agent_events"][0]
        event = AgentEvent.model_validate_json(event_row[3])
        event_claims = json.loads(before["agent_event_publications"][0][2])
        event_claims.pop("event_schema_version")
        proof.verify_event(
            json.dumps(event_claims).encode(), event_row[3].encode(), event.event_id, tid, 1
        )
        projection_claims = json.loads(before["agent_projection_publications"][0][1])
        projection_claims.pop("projection_version")
        assert (
            proof.verify_projection(json.dumps(projection_claims).encode(), tid).projection_version
            == 20
        )
        assert business_rows(store.path) == before


async def test_v20_projection_cannot_declare_a_new_fork_marker_as_original_v1(tmp_path):
    with binding() as proof:
        _, _, child, _ = await authenticated_v2_fork(tmp_path / "fork-marker.db", proof, False, 1)
        assert isinstance(child.fork_snapshot, ThreadForkSnapshotV2)
        assert child.fork_snapshot.items == ()
        with raises_code("publication_history_unproven"):
            require_projection_content_version(child, 20)


async def authenticated_v2_fork(path, proof, with_rejections, generations):
    """沿正式Fork CAS创建v2及再继承历史，不使用构造绕过或替代合同。"""
    store, tid, source = await seed(path, proof)
    if with_rejections:
        turn_id, sequence = await calling_model(store, tid, 1)
        rejected = await store.append(tid, rejection_batch(turn_id), expected_sequence=sequence)
        source = await store.append(
            tid,
            [
                EventDraft(turn_id=turn_id, payload=UsageRecorded(step=1, usage=Usage())),
                EventDraft(turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.FINALIZING)),
                EventDraft(turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.COMPLETED)),
            ],
            expected_sequence=rejected.sequence,
        )
    for generation in range(generations):
        snapshot = prepare_fork_snapshot(
            source,
            request_id=f"authenticated-v2-{generation}",
            through_turn_id=None,
            policy=ToolResultViewPolicy(),
            spec_version="harnessix.thread-fork/v2",
        ).snapshot
        draft = EventDraft(payload=ThreadForked(workspace=source.workspace, snapshot=snapshot))
        child = await store.fork(
            source.thread_id, uuid4(), draft, expected_source_sequence=source.sequence
        )
        if generation + 1 < generations:
            source = child
    return store, source, child, draft


@pytest.mark.parametrize("with_rejections", [False, True])
@pytest.mark.parametrize("generations", [1, 2])
async def test_v2_nested_history_authenticates_writes_reads_rebuild_and_retry(
    tmp_path, with_rejections, generations
):
    with binding() as proof:
        store, source, child, draft = await authenticated_v2_fork(
            tmp_path / "fork-v2.db", proof, with_rejections, generations
        )
        snapshot = child.fork_snapshot
        assert isinstance(snapshot, ThreadForkSnapshotV2)
        assert snapshot.spec_version == "harnessix.thread-fork/v2"
        assert bool(snapshot.items) is with_rejections
        assert (
            any(isinstance(item.content, ToolCallRejectionContent) for item in snapshot.items)
            is with_rejections
        )
        before = business_rows(store.path)
        frame = await read(store, child.thread_id)
        assert frame.thread == await store.get_thread(child.thread_id) == child
        assert len(frame.events) == 1 and frame.events[0].schema_version == 21
        assert isinstance(frame.events[0].payload.snapshot, ThreadForkSnapshotV2)
        version, seal = projection_rows(store.path, child.thread_id)
        assert (
            version == ProjectionPublicationSeal.model_validate_json(seal).projection_version == 21
        )
        assert await store.rebuild(child.thread_id) == child
        assert (
            await store.fork(
                source.thread_id,
                child.thread_id,
                draft,
                expected_source_sequence=source.sequence,
            )
            == child
        )
        assert business_rows(store.path) == before


@pytest.mark.parametrize("generations", [1, 2])
async def test_original_projection_proof20_rejects_even_empty_real_v2_history(
    tmp_path, generations
):
    with binding() as proof:
        store, _, child, _ = await authenticated_v2_fork(
            tmp_path / "v2-old-proof.db", proof, False, generations
        )
        assert isinstance(child.fork_snapshot, ThreadForkSnapshotV2)
        assert child.fork_snapshot.items == ()
        assert (await read(store, child.thread_id)).thread == child
        _, seal = projection_rows(store.path, child.thread_id)
        claims = json.loads(seal)
        claims["projection_version"] = 20
        with closing(sqlite3.connect(store.path)) as database, database:
            database.execute(
                "UPDATE agent_threads SET projection_version=20 WHERE thread_id=?",
                (str(child.thread_id),),
            )
            database.execute(
                "UPDATE agent_projection_publications SET seal=? WHERE thread_id=?",
                (fixture_seal(claims, STORE_DOMAIN), str(child.thread_id)),
            )
        before = business_rows(store.path)
        with raises_code("publication_history_unproven"):
            await store.get_thread(child.thread_id)
        with raises_code("publication_history_unproven"):
            await read(store, child.thread_id)
        assert business_rows(store.path) == before


async def test_original_event_seal20_cannot_hide_even_empty_real_v2_history(tmp_path):
    with binding() as proof:
        store, _, child, _ = await authenticated_v2_fork(
            tmp_path / "v2-old-event.db", proof, False, 1
        )
        event = (await store.events(child.thread_id))[0]
        body = event.model_copy(update={"schema_version": 20}).model_dump_json().encode()
        seal = fixture_event_seal(proof, event, body, 20)
        with raises_code("publication_history_unproven"):
            proof.verify_event(seal, body, event.event_id, child.thread_id, event.sequence)


async def test_new_event_writer_only_issues_v21_and_preserves_original_mac_domain(tmp_path):
    with binding() as proof:
        event = AgentEvent(
            thread_id=uuid4(), sequence=1, payload=ThreadCreated(workspace=tmp_path.as_posix())
        )
        body, seal = await proof._events.issue_new_event(proof._store_id, event, CancelToken())
        assert seal.event_schema_version == json.loads(body)["schema_version"] == 21
        claims = json.loads(fixture_event_seal(proof, event, body, 21))
        assert claims == seal.model_dump(mode="json")
        with raises_code("publication_event_invalid"):
            await proof.issue_event(event.model_copy(update={"schema_version": 20}))


def test_v20_declaration_cannot_hide_rejection_content_or_reentry(tmp_path):
    with binding() as proof:
        for payload in (
            ItemStarted(
                item_id=uuid4(),
                content=ToolCallRejectionContent(
                    call_id=uuid4(), provider_call_id="rejected", model_step=1
                ),
            ),
            TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT, reason="tool_rejection"),
        ):
            event = AgentEvent(thread_id=uuid4(), sequence=1, turn_id=uuid4(), payload=payload)
            body = event.model_copy(update={"schema_version": 20}).model_dump_json().encode()
            seal = fixture_event_seal(proof, event, body, 20)
            with raises_code("publication_history_unproven"):
                proof.verify_event(seal, body, event.event_id, event.thread_id, 1)


@pytest.mark.parametrize("row_version", [19, 20, 21, 22])
@pytest.mark.parametrize("claim_version", [20, 21])
async def test_projection_row_and_authenticated_claim_must_match(
    tmp_path, row_version, claim_version
):
    with binding() as proof:
        store, tid, original = await seed(tmp_path / "projection.db", proof)
        _, seal = projection_rows(store.path)
        claims = json.loads(seal)
        claims["projection_version"] = claim_version
        with closing(sqlite3.connect(store.path)) as database, database:
            database.execute("UPDATE agent_threads SET projection_version=?", (row_version,))
            database.execute(
                "UPDATE agent_projection_publications SET seal=?",
                (fixture_seal(claims, STORE_DOMAIN),),
            )
        if row_version == claim_version:
            assert await store.get_thread(tid) == original
        else:
            with raises_code("publication_history_unproven"):
                await store.get_thread(tid)


@pytest.mark.parametrize("authenticated", [False, True])
async def test_nonadjacent_rejection_pairs_commit_atomically_and_idempotently(
    tmp_path, authenticated
):
    with binding() as proof:
        store = SQLiteSessionStore(
            tmp_path / "batch.db", publication=proof if authenticated else None
        )
        await store.initialize()
        tid = uuid4()
        await store.append(
            tid,
            [EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))],
            expected_sequence=0,
        )
        turn_id, sequence = await calling_model(store, tid, 1)
        batch = rejection_batch(turn_id)
        thread = await store.append(tid, batch, expected_sequence=sequence)
        before = business_rows(store.path)
        assert await store.append(tid, batch, expected_sequence=sequence) == thread
        assert business_rows(store.path) == before
        assert await store.get_thread(tid) == replay(await store.events(tid)) == thread
        assert all(event.schema_version == 21 for event in await store.events(tid))
        assert before["agent_threads"][0][-1] == 21
        if authenticated:
            version, seal = projection_rows(store.path)
            assert version == ProjectionPublicationSeal.model_validate_json(seal).projection_version
            assert all(
                EventPublicationSeal.model_validate_json(row[2]).event_schema_version == 21
                for row in before["agent_event_publications"]
            )
        with closing(sqlite3.connect(store.path)) as database, database:
            database.execute("UPDATE agent_threads SET projection_version=20")
            if authenticated:
                claims = json.loads(projection_rows(store.path)[1])
                claims["projection_version"] = 20
                database.execute(
                    "UPDATE agent_projection_publications SET seal=?",
                    (fixture_seal(claims, STORE_DOMAIN),),
                )
        with raises_code("publication_history_unproven"):
            await store.get_thread(tid)


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("keep", [1, 2, 3])
async def test_orphan_or_open_rejection_rolls_back_entire_batch(tmp_path, authenticated, keep):
    with binding() as proof:
        store = SQLiteSessionStore(
            tmp_path / "orphan.db", publication=proof if authenticated else None
        )
        await store.initialize()
        tid = uuid4()
        await store.append(
            tid,
            [EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))],
            expected_sequence=0,
        )
        turn_id, sequence = await calling_model(store, tid, 1)
        before = business_rows(store.path)
        with raises_code("invalid_event"):
            await store.append(tid, rejection_batch(turn_id, 1)[:keep], expected_sequence=sequence)
        assert business_rows(store.path) == before
        assert (await store.get_thread(tid)).sequence == sequence


@pytest.mark.parametrize("authenticated", [False, True])
async def test_snapshot_consumption_rejects_authenticated_orphan_rejection(tmp_path, authenticated):
    with binding() as proof:
        store = SQLiteSessionStore(
            tmp_path / "snapshot.db", publication=proof if authenticated else None
        )
        await store.initialize()
        tid = uuid4()
        await store.append(
            tid,
            [EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))],
            expected_sequence=0,
        )
        turn_id, sequence = await calling_model(store, tid, 1)
        await store.append(tid, rejection_batch(turn_id, 1), expected_sequence=sequence)
        with closing(sqlite3.connect(store.path)) as database, database:
            snapshot = json.loads(
                database.execute("SELECT snapshot_json FROM agent_threads").fetchone()[0]
            )
            snapshot["turns"][0]["items"] = snapshot["turns"][0]["items"][:-1]
            body = json.dumps(snapshot, ensure_ascii=False)
            digest = hashlib.sha256(body.encode()).hexdigest()
            database.execute(
                "UPDATE agent_threads SET snapshot_json=?,snapshot_sha256=?", (body, digest)
            )
            if authenticated:
                claims = json.loads(projection_rows(store.path)[1])
                claims["snapshot_sha256"] = digest
                database.execute(
                    "UPDATE agent_projection_publications SET seal=?",
                    (fixture_seal(claims, STORE_DOMAIN),),
                )
        before = business_rows(store.path)
        with raises_code("publication_history_unproven" if authenticated else "projection_corrupt"):
            await store.get_thread(tid)
        assert business_rows(store.path) == before


@pytest.mark.parametrize("point", ["session.after_events", "session.after_projection"])
async def test_rejection_batch_commit_fault_does_not_publish_any_partial_fact(tmp_path, point):
    with binding() as proof:
        store, tid, _ = await seed(tmp_path / "fault.db", proof)
        turn_id, sequence = await calling_model(store, tid, 1)
        before = business_rows(store.path)

        def fail(actual):
            if actual == point:
                raise RuntimeError("离线提交故障")

        store._fault = fail
        with pytest.raises(RuntimeError, match="离线提交故障"):
            await store.append(tid, rejection_batch(turn_id), expected_sequence=sequence)
        assert business_rows(store.path) == before


async def test_frozen_v30_reader_fails_closed_on_v31_marker_without_writes(tmp_path, monkeypatch):
    with binding() as proof:
        store, _, _ = await seed(tmp_path / "reader.db", proof)
        before = business_rows(store.path)
        resources = files("harnessix.session.migrations")
        frozen_v30 = tuple(
            resource
            for resource in resources.iterdir()
            if resource.name.endswith(".sql")
            and int(resource.name.split("_", 1)[0]) in range(1, 31)
        )
        assert len(frozen_v30) == 30
        assert {
            int(resource.name.split("_", 1)[0]): hashlib.sha256(resource.read_bytes()).hexdigest()
            for resource in frozen_v30
        } == dict(enumerate(_SESSION_MIGRATION_CHECKSUMS[:30], 1))
        assert before["agent_migrations"] == list(enumerate(_SESSION_MIGRATION_CHECKSUMS, 1))
        assert json.loads(before["agent_events"][0][3])["schema_version"] == 21

        class V30Resources:
            def iterdir(self):
                # 固定旧Reader完整v30集合，不能仅排除0031后暗中接纳未来迁移。
                return iter(frozen_v30)

        monkeypatch.setattr(sqlite_module, "files", lambda _: V30Resources())
        with raises_code("schema_too_new"):
            await store.initialize()
        assert business_rows(store.path) == before


async def test_future_projection_version_still_fails_closed_without_publication(tmp_path):
    store = SQLiteSessionStore(tmp_path / "future.db")
    await store.initialize()
    tid = uuid4()
    await store.append(
        tid, [EventDraft(payload=ThreadCreated(workspace=tmp_path.as_posix()))], expected_sequence=0
    )
    with closing(sqlite3.connect(store.path)) as database, database:
        database.execute("UPDATE agent_threads SET projection_version=22")
    with raises_code("projection_too_new"):
        await store.get_thread(tid)
