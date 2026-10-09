"""真实 Store/CAS 的观察控制与坏数据同码负控，不替换原编解码或物理 IO。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import store as delivery_store
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_record_codec import decode_workspace_record
from harnessix.domain.models import ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.blob_read_control import (
    BlobErrorMarker,
    WorkspaceBlobReader,
    read_workspace_blob,
)
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.parent_closure_contracts import WorkspaceParentClosureManifest
from tests.delivery.test_workspace_record_v3 import _payload, _prepared
from tests.execution.test_store import _approval
from tests.trusted_actions.test_parent_closure_store import parent_route


class _BlobProbe:
    """只记录原读取；故障仅从 Transaction 构造时的观察回调抛出。"""

    def __init__(self) -> None:
        self.active: tuple[str, str] | None = None
        self.reset()

    def reset(self) -> None:
        assert self.active is None
        self.failure: tuple[str, str, BaseException] | None = None
        self.requests: list[str] = []
        self.controlled_requests: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.physical_errors: list[BaseException] = []
        self.marks: list[UpstreamCheckpointError] = []

    def observe(self) -> None:
        if self.active is None:
            return
        digest, edge = self.active
        self.events.append((edge, digest))
        if self.failure is not None and self.failure[:2] == self.active:
            raise self.failure[2]

    def install(self, store: SQLiteWorkspaceTransactionStore, monkeypatch) -> None:
        original_checked = delivery_store._read_checked_blob
        original_read = store._read_blob
        original_controlled = store.controlled_blob

        def checked(digest: str, read: Callable[[str], bytes], check: Callable[[], None]) -> bytes:
            assert self.active is None
            self.requests.append(digest)
            self.active = (digest, "before")
            try:
                return original_checked(digest, read, check)
            finally:
                self.active = None

        def physical_read(digest: str) -> bytes:
            assert self.active == (digest, "before")
            self.events.append(("read", digest))
            try:
                body = original_read(digest)
            except BaseException as error:
                self.physical_errors.append(error)
                raise
            self.events.append(("verified", digest))
            self.active = (digest, "after")
            return body

        def controlled(digest: str, mark_error: BlobErrorMarker) -> bytes:
            self.controlled_requests.append(digest)

            def mark(error: BaseException) -> UpstreamCheckpointError:
                marked = mark_error(error)
                assert marked.error is error
                self.marks.append(marked)
                return marked

            return original_controlled(digest, mark)

        # 两个公开入口、共享 helper 和物理 Reader 都继续调用原实现。
        monkeypatch.setattr(delivery_store, "_read_checked_blob", checked)
        monkeypatch.setattr(store, "_read_blob", physical_read)
        monkeypatch.setattr(store, "controlled_blob", controlled)


@dataclass(frozen=True)
class _Consumer:
    consume: Callable[[], object]
    transactions: SQLiteWorkspaceTransactionStore
    reader: WorkspaceBlobReader
    probe: _BlobProbe
    digests: tuple[str, ...]
    manifest_digest: str
    databases: tuple[sqlite3.Connection, ...]
    bad_data_code: str

    def target(self, location: str) -> str:
        return self.manifest_digest if location == "manifest" else self.digests[-1]


def _closure_digests(store: SQLiteWorkspaceTransactionStore, digest: str) -> tuple[str, ...]:
    manifest = WorkspaceParentClosureManifest.model_validate_json(
        store._read_blob(digest), strict=True
    )
    assert manifest.chunks
    return (digest, *(chunk.sha256 for chunk in manifest.chunks))


@pytest.fixture(
    params=[
        "codec_decode",
        "execution_save",
        "execution_load",
        "execution_approval",
        "audit_save",
        "audit_load",
        "audit_transition",
    ]
)
def consumer(request, tmp_path: Path, monkeypatch) -> Iterator[_Consumer]:
    kind, operation = request.param.split("_", 1)
    probe = _BlobProbe()
    observer = probe.observe
    with ExitStack() as resources:
        transactions = resources.enter_context(
            SQLiteWorkspaceTransactionStore(tmp_path / "transactions", checkpoint=observer)
        )
        databases = [transactions._db]
        if kind == "codec":
            record = transactions.save(_prepared(tmp_path / "workspace", transactions, count=3))
            payload = _payload(transactions, record)
            manifest_digest = record.plan.source.parent_closure.sha256
            digests = (
                json.loads(payload)["plan_ref"]["sha256"],
                *_closure_digests(transactions, manifest_digest),
            )
        else:
            route, blobs = parent_route(tmp_path / "workspace", leaves=3)
            for digest, body in blobs.items():
                transactions.put_blob(digest, body)
            manifest_digest = route.execution.workspace.parent_closure.sha256
            digests = _closure_digests(transactions, manifest_digest)

        probe.install(transactions, monkeypatch)
        reader = WorkspaceBlobReader(transactions.blob, transactions.controlled_blob)
        if kind == "codec":
            consume = partial(decode_workspace_record, payload, reader)
            bad_data_code = "delivery_store_corrupt"
        elif kind == "execution":
            plans = resources.enter_context(
                SQLiteExecutionPlanStore(tmp_path / "plans.db", read_blob=reader)
            )
            databases.append(plans._db)
            plan = route.execution
            if operation != "save":
                plans.save_plan(plan)
            if operation == "save":
                consume = partial(plans.save_plan, plan)
            elif operation == "load":
                consume = partial(plans.load_plan, plan.plan_id)
            else:
                approval = _approval(plan)
                consume = partial(plans.record_approval, approval)
            bad_data_code = (
                "execution_plan_invalid" if operation == "save" else "execution_store_corrupt"
            )
        else:
            audit = resources.enter_context(
                SQLiteActionAuditStore(tmp_path / "audit.db", read_blob=reader)
            )
            databases.append(audit._db)
            if operation != "save":
                audit.save_plan(route, initial_state="pending_approval")
            if operation == "save":
                consume = partial(audit.save_plan, route, initial_state="pending_approval")
            elif operation == "load":
                consume = partial(audit.load, route.execution.plan_id)
            else:
                consume = partial(
                    audit.transition,
                    route.execution.plan_id,
                    expected={"pending_approval"},
                    target="ready",
                    approval_outcome=ApprovalOutcome.APPROVED,
                    approval_actor="reviewer",
                )
            bad_data_code = (
                "action_route_plan_invalid" if operation == "save" else "action_audit_store_corrupt"
            )
        probe.reset()
        yield _Consumer(
            consume=consume,
            transactions=transactions,
            reader=reader,
            probe=probe,
            digests=digests,
            manifest_digest=manifest_digest,
            databases=tuple(databases),
            bad_data_code=bad_data_code,
        )
        assert transactions._checkpoint is observer


@contextmanager
def _no_database_writes(databases: tuple[sqlite3.Connection, ...]) -> Iterator[None]:
    before = [(db, db.total_changes, tuple(db.iterdump())) for db in databases]
    statements: list[str] = []
    for db in databases:
        db.set_trace_callback(statements.append)
    try:
        yield
    finally:
        for db in databases:
            db.set_trace_callback(None)
    assert not any(
        statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
        for statement in statements
    ), statements
    for db, changes, rows in before:
        assert not db.in_transaction
        assert db.total_changes == changes
        assert tuple(db.iterdump()) == rows


def _control_failure(code: str, nested: bool) -> BaseException:
    failure: BaseException = KernelError(code, "原 Transaction 观察控制，不是 CAS 坏数据")
    if nested:
        return UpstreamCheckpointError(UpstreamCheckpointError(failure))
    return failure


def _read_events(digests: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (event, digest) for digest in digests for event in ("before", "read", "verified", "after")
    ]


def _failed_prefix(consumer: _Consumer, digest: str) -> tuple[str, ...]:
    return consumer.digests[: consumer.digests.index(digest) + 1]


# 每个物理边界都覆盖两种同码来源；交错裸／嵌套，避免无目的全笛卡尔积。
@pytest.mark.parametrize(
    "code,location,edge,nested",
    [
        pytest.param(
            "delivery_blob_corrupt", "manifest", "before", False, id="manifest-before-bare"
        ),
        pytest.param(
            "delivery_blob_invalid", "manifest", "before", True, id="manifest-before-nested"
        ),
        pytest.param("delivery_blob_invalid", "manifest", "after", False, id="manifest-after-bare"),
        pytest.param(
            "delivery_blob_corrupt", "manifest", "after", True, id="manifest-after-nested"
        ),
        pytest.param("delivery_blob_invalid", "last_chunk", "before", False, id="last-before-bare"),
        pytest.param(
            "delivery_blob_corrupt", "last_chunk", "before", True, id="last-before-nested"
        ),
        pytest.param("delivery_blob_corrupt", "last_chunk", "after", False, id="last-after-bare"),
        pytest.param("delivery_blob_invalid", "last_chunk", "after", True, id="last-after-nested"),
    ],
)
def test_original_observer_keeps_first_error_and_exact_cas_prefix(
    consumer: _Consumer, code: str, location: str, edge: str, nested: bool
) -> None:
    digest = consumer.target(location)
    failure = _control_failure(code, nested)
    consumer.probe.failure = (digest, edge, failure)
    with _no_database_writes(consumer.databases):
        with pytest.raises(type(failure)) as caught:
            consumer.consume()
        assert caught.value is failure

    prefix = _failed_prefix(consumer, digest)
    assert tuple(consumer.probe.requests) == prefix
    assert tuple(consumer.probe.controlled_requests) == prefix
    expected_events = _read_events(prefix[:-1])
    expected_events += [("before", digest)] if edge == "before" else _read_events((digest,))
    assert consumer.probe.events == expected_events
    assert not consumer.probe.physical_errors
    assert len(consumer.probe.marks) == 1
    assert consumer.probe.marks[0].error is failure
    assert consumer.probe.active is None


@pytest.mark.parametrize("location", ["manifest", "last_chunk"])
@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_real_cas_damage_is_unmarked_and_keeps_original_bad_data_classification(
    consumer: _Consumer, location: str, damage: str
) -> None:
    digest = consumer.target(location)
    path = consumer.transactions._blobs / digest
    if damage == "missing":
        path.unlink()
    else:
        body = path.read_bytes()
        path.write_bytes(bytes([body[0] ^ 1]) + body[1:])  # 同长度、同权限，真实 SHA 损坏。

    def unexpected_marker(error: BaseException) -> UpstreamCheckpointError:
        pytest.fail(f"真实 CAS 坏数据不得调用控制标记：{error!r}")

    with _no_database_writes(consumer.databases):
        with pytest.raises(KernelError) as physical:
            read_workspace_blob(consumer.reader, digest, unexpected_marker)
        assert physical.value.code == "delivery_blob_corrupt"
        assert len(consumer.probe.physical_errors) == 1
        assert physical.value is consumer.probe.physical_errors[0]
        assert consumer.probe.events == [("before", digest), ("read", digest)]
        assert not consumer.probe.marks
        consumer.probe.reset()
        with pytest.raises(KernelError) as caught:
            consumer.consume()
        assert caught.value.code == consumer.bad_data_code

    prefix = _failed_prefix(consumer, digest)
    assert tuple(consumer.probe.requests) == prefix
    assert tuple(consumer.probe.controlled_requests) == prefix
    assert consumer.probe.events == _read_events(prefix[:-1]) + [
        ("before", digest),
        ("read", digest),
    ]
    assert len(consumer.probe.physical_errors) == 1
    assert not consumer.probe.marks
    assert consumer.probe.active is None


@pytest.mark.parametrize("code", ["delivery_blob_corrupt", "delivery_blob_invalid"])
@pytest.mark.parametrize("edge", ["before", "after"])
@pytest.mark.parametrize("nested", [False, True], ids=["bare", "nested"])
def test_default_blob_keeps_original_error_without_new_wrapper(
    tmp_path: Path, monkeypatch, code: str, edge: str, nested: bool
) -> None:
    probe = _BlobProbe()
    with SQLiteWorkspaceTransactionStore(
        tmp_path / "transactions", checkpoint=probe.observe
    ) as store:
        prepared = _prepared(tmp_path / "workspace", store, count=3)
        digest = prepared.plan.source.parent_closure.sha256
        body = store._read_blob(digest)
        probe.install(store, monkeypatch)
        reader = WorkspaceBlobReader(store.blob, store.controlled_blob)
        with _no_database_writes((store._db,)):
            assert store.blob(digest) == body
            original_events = list(probe.events)
            assert original_events == _read_events((digest,))
            assert not probe.controlled_requests
            probe.reset()
            assert read_workspace_blob(reader, digest, UpstreamCheckpointError) == body
            assert probe.events == original_events
            assert probe.controlled_requests == [digest]
            assert not probe.marks
            probe.reset()
            failure = _control_failure(code, nested)
            probe.failure = (digest, edge, failure)
            with pytest.raises(type(failure)) as caught:
                store.blob(digest)
            assert caught.value is failure
        assert probe.requests == [digest]
        assert probe.events == (
            [("before", digest)] if edge == "before" else _read_events((digest,))
        )
        assert not probe.controlled_requests
        assert not probe.marks
        assert not probe.physical_errors
        assert probe.active is None


def test_invalid_digest_is_not_marked_as_observation_control(tmp_path: Path, monkeypatch) -> None:
    probe = _BlobProbe()
    with SQLiteWorkspaceTransactionStore(
        tmp_path / "transactions", checkpoint=probe.observe
    ) as store:
        probe.install(store, monkeypatch)
        reader = WorkspaceBlobReader(store.blob, store.controlled_blob)
        with _no_database_writes((store._db,)):
            with pytest.raises(KernelError) as caught:
                read_workspace_blob(reader, "not-a-digest", UpstreamCheckpointError)
            assert caught.value.code == "delivery_blob_invalid"
            assert len(probe.physical_errors) == 1
            assert caught.value is probe.physical_errors[0]
        assert probe.controlled_requests == ["not-a-digest"]
        assert probe.events == [("before", "not-a-digest"), ("read", "not-a-digest")]
        assert not probe.marks
