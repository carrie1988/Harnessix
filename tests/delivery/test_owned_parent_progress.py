"""真实事务／Route 历史的操作局部进度，原构造观察与控制来源不能丢失。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.delivery.workspace_record_codec import decode_workspace_record
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.delivery.test_workspace_record_v3 import _prepared
from tests.trusted_actions.test_parent_closure_store import parent_route, read_blobs


class _Progress:
    def __init__(self):
        self.active = False
        self.events = []
        self.failure = None
        self.error = None
        self.read_error = None
        self.failed_digest = None

    def record(self, name):
        self.events.append(name)
        if self.failure == name:
            raise self.error

    def full(self):
        assert not self.active
        self.record("full")

    def local(self):
        assert self.active
        self.record("local")

    def observer(self):
        self.record("observer-local" if self.active else "observer-full")

    @contextmanager
    def pure(self):
        self.record("entry")
        self.active = True
        try:
            yield self.local
        finally:
            self.active = False
        self.record("exit")


ERRORS = (
    "cancel",
    "task",
    "deadline",
    "os",
    "value",
    "type",
    "closure",
    "store",
    "audit",
    "nested",
)


def _error(name):
    if name == "cancel":
        return TurnCancelled("original control")
    if name == "task":
        return asyncio.CancelledError("original control")
    if name in {"closure", "store", "audit"}:
        return KernelError(
            {
                "closure": "workspace_closure_corrupt",
                "store": "delivery_store_corrupt",
                "audit": "action_audit_store_corrupt",
            }[name],
            "control, not data",
        )
    if name == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original")))
    return {"deadline": TimeoutError, "os": OSError, "value": ValueError, "type": TypeError}[name](
        "original control"
    )


@pytest.fixture(params=["codec", "store", "audit"])
def consumer(request, tmp_path, monkeypatch):
    progress, reads = _Progress(), []
    progress.needs_observer = request.param == "codec"
    if request.param == "audit":
        route, blobs = parent_route(tmp_path / "root", leaves=3)
        with SQLiteActionAuditStore(tmp_path / "audit.db", read_blob=read_blobs(blobs)) as store:
            expected = store.save_plan(route, initial_state="pending_approval")
            original = store._read_blob

            def read(digest):
                assert not progress.active
                reads.append(digest)
                if progress.read_error is not None and (
                    progress.failed_digest is None or digest == progress.failed_digest
                ):
                    raise progress.read_error
                return original(digest)

            monkeypatch.setattr(store, "_read_blob", read)
            monkeypatch.setattr(store, "_checkpoint", progress.observer)

            def load(**options):
                return store.load(route.execution.plan_id, checkpoint=progress.full, **options)

            yield progress, reads, load, expected, store
    else:
        with SQLiteWorkspaceTransactionStore(tmp_path / "transactions") as store:
            record = store.save(_prepared(tmp_path / "root", store, count=3))
            payload = store._db.execute("SELECT payload FROM workspace_transactions").fetchone()[0]
            original = store._read_blob

            def read(digest):
                assert not progress.active
                reads.append(digest)
                if progress.read_error is not None and (
                    progress.failed_digest is None or digest == progress.failed_digest
                ):
                    raise progress.read_error
                return original(digest)

            monkeypatch.setattr(store, "_read_blob", read)
            monkeypatch.setattr(store, "_checkpoint", progress.observer)

            def load(**options):
                if request.param == "codec":
                    return decode_workspace_record(
                        payload, read, checkpoint=progress.full, **options
                    ).record
                return store.load(record.transaction_id, checkpoint=progress.full, **options)

            yield progress, reads, load, record, store


def test_original_complete_reads_and_store_observer_are_preserved(consumer):
    progress, reads, load, expected, store = consumer
    statements = []
    store._db.set_trace_callback(statements.append)
    assert load() == expected
    old_reads, old_observers = tuple(reads), sum(e.startswith("observer-") for e in progress.events)
    reads.clear()
    progress.events.clear()
    assert load(pure_progress=progress.pure) == expected
    assert tuple(reads) == old_reads
    assert sum(e.startswith("observer-") for e in progress.events) == old_observers
    assert progress.events.count("entry") == progress.events.count("exit") == 2
    assert "local" in progress.events and not progress.active
    assert not any(
        s.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT"}
        for s in statements
    )


@pytest.mark.parametrize("name", ERRORS)
@pytest.mark.parametrize("phase", ["entry", "local", "exit"])
def test_first_pure_control_error_keeps_identity_through_original_data_mapping(
    consumer, name, phase
):
    progress, _, load, _, _ = consumer
    progress.failure, progress.error = phase, _error(name)
    with pytest.raises(BaseException) as caught:
        load(pure_progress=progress.pure)
    assert caught.value is progress.error
    assert progress.events[-1] == phase and not progress.active


@pytest.mark.parametrize("name", ERRORS)
def test_original_observer_error_inside_pure_is_not_mapped_to_bad_history(consumer, name):
    progress, _, load, _, store = consumer
    if progress.needs_observer:
        # Codec 本身没有 Store 观察；显式组合模拟其实际 Store 调用方。
        @contextmanager
        def factory():
            with progress.pure() as local:

                def check():
                    progress.observer()
                    local()

                yield check
    else:
        factory = progress.pure
    progress.failure, progress.error = "observer-local", _error(name)
    with pytest.raises(BaseException) as caught:
        load(pure_progress=factory)
    assert caught.value is progress.error and not progress.active
    assert not store._db.in_transaction


@pytest.mark.parametrize("name", ERRORS)
@pytest.mark.parametrize("layered", [False, True])
def test_actual_cas_port_error_keeps_exact_object_and_read_prefix(consumer, name, layered):
    progress, reads, load, _, store = consumer
    progress.read_error = _error(name)
    with pytest.raises(BaseException) as caught:
        load(**({"pure_progress": progress.pure} if layered else {}))
    assert caught.value is progress.read_error
    assert len(reads) == 1 and "local" not in progress.events
    assert not progress.active and not store._db.in_transaction


@pytest.mark.parametrize("name", ERRORS)
@pytest.mark.parametrize("layered", [False, True])
@pytest.mark.parametrize("phase", ["manifest", "last_chunk"])
def test_closure_cas_failure_preserves_exact_digest_prefix_and_object(
    consumer, name, layered, phase
):
    progress, reads, load, expected, store = consumer
    assert load() == expected
    complete_reads = tuple(reads)
    # Audit 没有 Plan Blob；另两条路径首先读 Plan，然后读 Manifest 和实际 Chunk。
    index = (
        (0 if len(complete_reads) == 2 else 1) if phase == "manifest" else len(complete_reads) - 1
    )
    progress.failed_digest = complete_reads[index]
    progress.read_error = _error(name)
    reads.clear()
    with pytest.raises(BaseException) as caught:
        load(**({"pure_progress": progress.pure} if layered else {}))
    assert caught.value is progress.read_error
    assert tuple(reads) == complete_reads[: index + 1]
    assert not progress.active and not store._db.in_transaction
