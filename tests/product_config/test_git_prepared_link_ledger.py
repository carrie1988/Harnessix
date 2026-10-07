"""真实原认证 SDK 到 GitDB prepared 关联；不批准或执行业务 Git 写入。"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store import SQLiteGitDeliveryStore
from harnessix.delivery.git_store_genesis import initialize_git_store_v2
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    initialize_git_prefix_genesis,
    publish_git_prefix_changes,
)
from harnessix.product_config.git_prepared_link_connection import open_prepared_git_connection
from harnessix.product_config.git_prepared_link_ledger import ProductGitPreparedLinkLedger
from harnessix.product_config.git_prepared_link_rows import prepared_link_columns
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.product_config.server import open_default_product_action_runtime
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_checkpoint_preparation import _pending
from tests.support.git_user_observation import run_authenticated_observation


async def _case(tmp_path, config, monkeypatch, inspect, *, fmt="sha1", continuous=False):
    owners = []

    @asynccontextmanager
    async def captured(*args, **kwargs):
        owners.append(kwargs["root_owner"])
        async with open_default_product_action_runtime(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(
        "harnessix.product_config.server.open_default_product_action_runtime", captured
    )

    async def observed(scenario):
        async def pending(actual, authorized, supervisor):
            assert authorized and not supervisor._store.active()
            await inspect(actual)

        await _pending(scenario, monkeypatch, owners[-1], pending)

    await run_authenticated_observation(
        tmp_path,
        config,
        monkeypatch,
        observed,
        object_format=fmt,
        continuous=continuous,
        explicit_git_ledger=True,
    )


@contextmanager
def _database(actual, *, path=None, read_only=False):
    path = path or actual.scenario.state / "git-delivery" / "git-delivery.db"
    if not read_only and not path.exists():
        # 目录和空 v1 文件仅由原 Store 准备；业务连接工厂本身绝不创建或迁移。
        if path.name == "git-delivery.db":
            SQLiteGitDeliveryStore(path.parent).close()
        else:
            # 故障注入副本不是产品账本，仅创建已有文件供工厂接管。
            path.touch(mode=0o600)
    with open_prepared_git_connection(path, read_only=read_only) as db:
        try:
            yield db
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")


def _ledger(actual, db):
    scenario = actual.scenario
    return ProductGitPreparedLinkLedger(
        db,
        scenario.router,
        actual.preparer.core_store,
        actual.artifacts,
        scenario.reader,
        snapshot_ports=scenario.router._snapshot_ports,
        workspace_scope=actual.provider._workspace_scope,
    )


async def _genesis(actual, db):
    publication = actual.scenario.session._publication
    with git_prefix_sql_window(db, checkpoint=lambda: None):
        db.execute("BEGIN IMMEDIATE")
        initialize_git_store_v2(db)
        await initialize_git_prefix_genesis(
            db,
            publication.git,
            publication.git_prefix,
            publication._events._protection,
            cancel=CancelToken(),
        )
        db.execute("COMMIT")


def _rows(db):
    tables = db.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
    ).fetchall()
    return tuple(
        (name, db.execute(f"SELECT * FROM {name} ORDER BY 1,2").fetchall()) for (name,) in tables
    )


async def _history(actual):
    """读取真实原认证历史，保留原接口的取消与绝对期限参数。"""
    return await actual.scenario.session.authenticated_thread_history(
        actual.scenario.thread.thread_id, cancel=CancelToken(), deadline=time.monotonic() + 60
    )


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("continuous", [False, True])
async def test_actual_prepared_link_commit_reopen_read_only_and_stable_retry(
    tmp_path, config, monkeypatch, fmt, continuous
):
    async def inspect(actual):
        scenario = actual.scenario
        before = _source_snapshot(scenario.root)
        history = await _history(actual)
        routes = scenario.router._audit.routes()
        with _database(actual) as db:
            await _genesis(actual, db)
            db.execute("BEGIN IMMEDIATE")
            ledger = _ledger(actual, db)
            link = await ledger.prepare(
                actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert db.in_transaction
            assert link.approval.decision is None and link.phase == "prepared"
            assert link.plan.core.call == actual.call
            assert len(link.plan.core.baseline.source.patches) == (2 if continuous else 1)
            db.execute("COMMIT")
            baseline = _rows(db)
            db.execute("BEGIN IMMEDIATE")
            total = db.total_changes
            assert (
                await ledger.prepare(
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
                == link
            )
            assert db.total_changes == total
            db.execute("COMMIT")
            assert _rows(db) == baseline
        with _database(actual, read_only=True) as ro:
            ro.execute("BEGIN")
            changes = ro.total_changes
            assert await _ledger(actual, ro).read_all(
                cancel=CancelToken(), checkpoint=lambda: None
            ) == (link,)
            assert ro.total_changes == changes == 0
            ro.execute("ROLLBACK")
            assert _rows(ro) == baseline
            ro.execute("BEGIN")
            with pytest.raises(KernelError, match="只读"):
                await _ledger(actual, ro).prepare(
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
        assert _source_snapshot(scenario.root) == before
        assert await _history(actual) == history
        assert scenario.router._audit.routes() == routes
        assert not list(actual.preparer.worktree_parent.iterdir())

    await _case(tmp_path, config, monkeypatch, inspect, fmt=fmt, continuous=continuous)


async def test_actual_rollback_then_retry_and_commit_confirmation_loss(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _database(actual) as db:
            await _genesis(actual, db)
            genesis = _rows(db)
            ledger = _ledger(actual, db)
            db.execute("BEGIN IMMEDIATE")
            first = await ledger.prepare(
                actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            db.execute("ROLLBACK")
            assert _rows(db) == genesis
            db.execute("BEGIN IMMEDIATE")
            retry = await ledger.prepare(
                actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            assert retry == first
            db.execute("COMMIT")
            # 模拟调用方提交后响应丢失；只查询原业务身份，不重新分配发布 epoch。
            sealed = _rows(db)
            db.execute("BEGIN IMMEDIATE")
            assert (
                await ledger.prepare(
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
                == first
            )
            db.execute("COMMIT")
            assert _rows(db) == sealed
            assert db.execute("SELECT COUNT(*) FROM git_product_link_events").fetchone() == (1,)

    await _case(tmp_path, config, monkeypatch, inspect)


async def _publish_raw(actual, db, columns):
    """原 Key 签发的错误业务材料负控；仅物理 MAC 阳性，不冒充业务认证生产者。"""
    publication = actual.scenario.session._publication
    with git_prefix_sql_window(db, checkpoint=lambda: None):
        window = begin_git_prefix_write(
            db, publication.git, publication.git_prefix, checkpoint=lambda: None
        )
        db.execute("INSERT INTO git_product_links VALUES (?,?,?,?,?,?,?,?,?,?,?)", columns)
        db.execute(
            "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
            (columns[0], columns[9], columns[8], columns[-1]),
        )
        from uuid import UUID

        claim = GitDeliveryRecordClaims(
            record_kind="product_link",
            record_id=UUID(columns[0]),
            publication_epoch=uuid4(),
            sequence=1,
            previous_sha256="0" * 64,
            route_id=UUID(columns[0]),
            delivery_id=UUID(columns[1]),
            thread_id=UUID(columns[2]),
            turn_id=UUID(columns[3]),
            call_id=UUID(columns[4]),
        )
        await publish_git_prefix_changes(
            window,
            (claim,),
            publication.git,
            publication.git_prefix,
            publication._events._protection,
            cancel=CancelToken(),
        )
        assert (
            read_git_prefix_catalog(
                db,
                publication.git_verifier,
                publication.git_prefix_verifier,
                checkpoint=lambda: None,
            ).revision
            == 1
        )


async def test_valid_original_mac_does_not_prove_prepared_business_semantics(
    tmp_path, config, monkeypatch
):
    variants = (
        "request",
        "column_core",
        "column_thread",
        "column_action",
        "phase",
        "extra",
        "whitespace",
    )

    async def inspect(actual):
        with _database(actual) as db:
            await _genesis(actual, db)
            genesis = _rows(db)
            ledger = _ledger(actual, db)
            db.execute("BEGIN IMMEDIATE")
            link = await ledger.prepare(
                actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
            )
            db.execute("ROLLBACK")
            body = encode_product_git_prepared_link(link, checkpoint=lambda: None)
            for variant in variants:
                assert _rows(db) == genesis
                columns = list(prepared_link_columns(link, body))
                payload = json.loads(body)
                if variant == "request":
                    payload["approval"]["request_fingerprint"] = "0" * 64
                elif variant == "column_core":
                    columns[6] = "0" * 64
                elif variant == "column_thread":
                    columns[2] = str(uuid4())
                elif variant == "column_action":
                    columns[5] = "commit"
                elif variant == "phase":
                    columns[8] = "approved"
                elif variant == "extra":
                    payload["untrusted"] = True
                columns[-1] = json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
                if variant == "whitespace":
                    columns[-1] += " "
                db.execute("BEGIN IMMEDIATE")
                await _publish_raw(actual, db, tuple(columns))
                poisoned = _rows(db)
                changes = db.total_changes
                with pytest.raises(KernelError):
                    await ledger.read_all(cancel=CancelToken(), checkpoint=lambda: None)
                assert _rows(db) == poisoned and db.total_changes == changes
                # 新写端也必须拒绝整个原 MAC 阳性、业务语义阴性的历史，不能补签修复。
                with pytest.raises(KernelError):
                    await ledger.prepare(
                        actual.route.plan.execution.plan_id,
                        cancel=CancelToken(),
                        checkpoint=lambda: None,
                    )
                assert _rows(db) == poisoned and db.total_changes == changes
                db.execute("ROLLBACK")

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_cancellation_callback_epoch_and_late_write_reject_without_persistent_link(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        from harnessix.product_config import git_prepared_link_ledger as module

        with _database(actual) as db:
            await _genesis(actual, db)
            original = _rows(db)
            ledger = _ledger(actual, db)
            for marker in (
                ValueError("synthetic"),
                TimeoutError("synthetic"),
                KernelError("synthetic", "合成错误"),
            ):
                db.execute("BEGIN IMMEDIATE")

                def failed(marker=marker):
                    raise marker

                with pytest.raises(type(marker)) as caught:
                    await ledger.prepare(
                        actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=failed
                    )
                assert caught.value is marker
                db.execute("ROLLBACK")
                assert _rows(db) == original
            db.execute("BEGIN IMMEDIATE")
            cancel = CancelToken()
            cancel.cancel()
            with pytest.raises(TurnCancelled):
                await ledger.prepare(
                    actual.route.plan.execution.plan_id, cancel=cancel, checkpoint=lambda: None
                )
            db.execute("ROLLBACK")
            assert _rows(db) == original

            for fault in ("epoch", "late_write", "task", "deadline"):
                db.execute("BEGIN IMMEDIATE")
                authenticate = module._authenticate

                async def changed(*args, authenticate=authenticate, fault=fault, **kwargs):
                    result = await authenticate(*args, **kwargs)
                    if fault == "epoch":
                        db.execute("ROLLBACK")
                        db.execute("BEGIN IMMEDIATE")
                    elif fault == "late_write":
                        db.execute("UPDATE git_delivery_metadata SET value='1'")
                    elif fault == "task":
                        import asyncio

                        raise asyncio.CancelledError
                    else:
                        args[3]._deadline = 0
                    return result

                with monkeypatch.context() as patch:
                    patch.setattr(module, "_authenticate", changed)
                    import asyncio

                    with pytest.raises((KernelError, asyncio.CancelledError)):
                        await ledger.prepare(
                            actual.route.plan.execution.plan_id,
                            cancel=CancelToken(),
                            checkpoint=lambda: None,
                        )
                db.execute("ROLLBACK")
                assert _rows(db) == original

    await _case(tmp_path, config, monkeypatch, inspect)
