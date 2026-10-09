"""原 SDK prepared 业务证明的 Route 分层；不以机制检查替代深层恢复验收。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.delivery import store as transaction_store
from harnessix.product_config import action_runtime
from harnessix.product_config import git_prepared_link_proof as proof
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.product_config.test_git_prepared_link_controls import (
    _business_unchanged,
    _persisted,
    _sealed,
)
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _ledger,
    _rows,
)


def _original_route_probes(monkeypatch, *, error=None):
    """构造时链式注入原观察；计算工厂及 CAS 探针仍调用原实现。"""
    active = False
    records = {"segments": 0, "local": 0, "observations": 0, "reads": [], "failed": False}
    created = {}
    original_factory = proof.same_task_io_git_authentication
    original_read = transaction_store._read_checked_blob

    def audit(*args, **kwargs):
        original_observer = kwargs.pop("checkpoint", None)

        def observe():
            if original_observer is not None:
                original_observer()
            if active:
                records["observations"] += 1
                if error is not None and records["observations"] == 2:
                    records["failed"] = True
                    raise error

        store = SQLiteActionAuditStore(*args, checkpoint=observe, **kwargs)
        created["store"], created["observer"] = store, observe
        return store

    @contextmanager
    def factory(control):
        nonlocal active
        with original_factory(control) as progress:
            assert not active
            records["segments"] += 1
            active = True

            def check():
                records["local"] += 1
                progress()

            try:
                yield check
            finally:
                active = False

    def checked_blob(digest, read, check):
        # 物理完整回读仍在计算段外，不将 CAS 偷换为内存结果。
        assert not active
        records["reads"].append(digest)
        return original_read(digest, read, check)

    monkeypatch.setattr(action_runtime, "SQLiteActionAuditStore", audit)
    monkeypatch.setattr(proof, "same_task_io_git_authentication", factory)
    monkeypatch.setattr(transaction_store, "_read_checked_blob", checked_blob)
    return records, created


async def test_original_prepared_commit_and_readback_use_route_progress_without_shared_mutation(
    tmp_path, config, monkeypatch
):
    records, created = _original_route_probes(monkeypatch)

    async def inspect(actual):
        scenario = actual.scenario
        async with _database(actual) as db:
            link, committed = await _sealed(actual, db)
            segments = records["segments"]
            observations = records["observations"]
            assert segments >= 2 and observations > 0 and records["local"] > 0
            before = scenario.unchanged_state()
            reads = len(records["reads"])
            db.execute("BEGIN")
            links = await _ledger(actual, db).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            )
            assert links == (link,)
            assert records["segments"] > segments
            assert records["observations"] > observations
            assert len(records["reads"]) > reads
            assert _rows(db) == committed
            db.execute("ROLLBACK")
            assert scenario.unchanged_state() == before
        assert scenario.router._audit is created["store"]
        assert scenario.router._audit._checkpoint is created["observer"]
        await _persisted(actual, committed)

    await _case(tmp_path, config, monkeypatch, inspect, continuous=True)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("code", ["workspace_closure_corrupt", "delivery_blob_corrupt"])
async def test_original_route_observer_first_failure_cannot_be_reclassified_or_publish_prefix(
    tmp_path, config, monkeypatch, nested, code
):
    error = KernelError(code, "original Audit observer, not corrupted Route data")
    if nested:
        error = UpstreamCheckpointError(UpstreamCheckpointError(error))
    records, created = _original_route_probes(monkeypatch, error=error)

    async def inspect(actual):
        async with _database(actual) as db:
            await _genesis(actual, db)
            baseline, changes = _rows(db), db.total_changes
            db.execute("BEGIN IMMEDIATE")
            with _business_unchanged(actual), pytest.raises(BaseException) as caught:
                await _ledger(actual, db).prepare(
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
            assert caught.value is error
            assert records["failed"] and records["segments"] == 1
            assert records["observations"] == 2 and records["local"] == 1
            assert _rows(db) == baseline and db.total_changes == changes
            assert db.in_transaction
            db.execute("ROLLBACK")
        assert actual.scenario.router._audit is created["store"]
        assert actual.scenario.router._audit._checkpoint is created["observer"]
        await _persisted(actual, baseline)

    await _case(tmp_path, config, monkeypatch, inspect, continuous=True)
