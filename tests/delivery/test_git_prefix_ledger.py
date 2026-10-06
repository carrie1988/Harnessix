"""真实SQLite、原Key/Scope及完整五kind物理账本；不表示产品桥接/Backup2已完成。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_contracts import ManagedGitWorktreeRecord
from harnessix.delivery.git_store_genesis import initialize_git_store_v2
from harnessix.product_config.git_prefix_catalog import (
    decode_git_prefix_catalog,
    encode_git_prefix_catalog,
)
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_rows import capture_git_prefix_rows
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    initialize_git_prefix_genesis,
    publish_git_prefix_changes,
)
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.session.store_publication import SessionPublicationBinding
from tests.delivery.test_git_parent_closure import _source
from tests.session.test_publication_seal import KEY, scope


@contextmanager
def _binding():
    with scope() as guard:
        binding = SessionPublicationBinding(uuid4(), uuid4(), KEY, guard)
        try:
            yield binding, guard
        finally:
            binding.close()


@contextmanager
def _connection(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    try:
        with git_prefix_sql_window(db, checkpoint=CancelToken().checkpoint):
            yield db
    finally:
        db.close()


async def _genesis(db, binding, guard):
    db.execute("BEGIN IMMEDIATE")
    initialize_git_store_v2(db)
    catalog = await initialize_git_prefix_genesis(
        db, binding.git, binding.git_prefix, guard, cancel=CancelToken()
    )
    db.execute("COMMIT")
    return catalog


def _link(db, *, sequence=0, phase="prepared", core=None, payload=None):
    """合成Link列用于物理认证测试，不冒充尚未接线的ProductGitDeliveryLink模型。"""
    core = core or tuple(uuid4() for _ in range(5))
    route, delivery, thread, turn, call = core
    payload = payload or json.dumps({"fixture": "private-link", "sequence": sequence})
    if sequence == 0:
        db.execute(
            "INSERT INTO git_product_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*map(str, core), "checkpoint", "a" * 64, "b" * 64, phase, sequence, payload),
        )
    else:
        db.execute(
            "UPDATE git_product_links SET phase=?,sequence=?,payload=? WHERE route_id=?",
            (phase, sequence, payload, str(route)),
        )
    db.execute(
        "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
        (str(route), sequence, phase, payload),
    )
    return core


def _claim(core, kind="product_link", record_id=None, *, sequence=1, previous="0" * 64, epoch=None):
    route, delivery, thread, turn, call = core
    return GitDeliveryRecordClaims(
        record_kind=kind,
        record_id=record_id or route,
        publication_epoch=epoch or uuid4(),
        sequence=sequence,
        previous_sha256=previous,
        route_id=route,
        delivery_id=delivery,
        thread_id=thread,
        turn_id=turn,
        call_id=call,
    )


async def _publish(db, binding, guard, window, claims):
    return await publish_git_prefix_changes(
        window, tuple(claims), binding.git, binding.git_prefix, guard, cancel=CancelToken()
    )


def _read(db, binding):
    db.execute("BEGIN")
    try:
        return read_git_prefix_catalog(
            db,
            binding.git_verifier,
            binding.git_prefix_verifier,
            checkpoint=CancelToken().checkpoint,
        )
    finally:
        db.execute("ROLLBACK")


async def test_genesis_and_actual_commit_reopen_with_original_verification_only(tmp_path):
    path = tmp_path / "git.db"
    with _binding() as (binding, guard):
        with _connection(path) as db:
            initial = await _genesis(db, binding, guard)
            assert initial.revision == 0
            db.execute("BEGIN IMMEDIATE")
            window = begin_git_prefix_write(
                db, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
            )
            core = _link(db)
            first = _claim(core)
            closed = await _publish(db, binding, guard, window, (first,))
            assert db.in_transaction
            db.execute("COMMIT")
            assert closed.revision == 1
            assert closed.genesis_epoch == initial.genesis_epoch
        # 真正关闭连接，新的Reader仅持原验真角色；不是当前Scope重签历史。
        with _connection(path) as reopened:
            assert _read(reopened, binding) == closed
            reopened.execute("BEGIN IMMEDIATE")
            second_window = begin_git_prefix_write(
                reopened, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
            )
            _link(reopened, sequence=1, phase="approved", core=core)
            second = _claim(
                core,
                sequence=2,
                previous=closed.streams[0].prefix_sha256,
                epoch=first.publication_epoch,
            )
            newest = await _publish(reopened, binding, guard, second_window, (second,))
            reopened.execute("COMMIT")
            assert newest.revision == 2 and newest.streams[0].count == 2
            assert _read(reopened, binding) == newest


@pytest.mark.parametrize(
    "kind", ["product_link", "object_inventory", "worktree_event", "checkpoint", "commit_event"]
)
async def test_every_fixed_record_kind_is_covered_without_new_auth_domain(tmp_path, kind):
    with _binding() as (binding, guard), _connection(tmp_path / "kind.db") as db:
        await _genesis(db, binding, guard)
        db.execute("BEGIN IMMEDIATE")
        window = begin_git_prefix_write(
            db, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
        )
        core = _link(db)
        root_claim = _claim(core)
        identity = uuid4()
        body = json.dumps({"fixture": kind, "private": "完整私有正文"})
        if kind == "product_link":
            additions = (root_claim,)
        else:
            if kind == "object_inventory":
                db.execute(
                    "INSERT INTO git_object_inventories VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(identity),
                        str(core[1]),
                        str(core[0]),
                        "materials_ready",
                        0,
                        "a" * 64,
                        "b" * 64,
                        body,
                    ),
                )
                db.execute(
                    "INSERT INTO git_object_inventory_events VALUES (?,?,?,?)",
                    (str(identity), 0, "materials_ready", body),
                )
            elif kind == "worktree_event":
                db.execute(
                    "INSERT INTO git_worktrees VALUES (?,?,?,?,?,?)",
                    (str(identity), str(uuid4()), "a" * 64, "prepared", 0, body),
                )
                db.execute(
                    "INSERT INTO git_worktree_events VALUES (?,?,?,?)",
                    (str(identity), 0, "prepared", body),
                )
            elif kind == "checkpoint":
                db.execute(
                    "INSERT INTO git_checkpoints VALUES (?,?,?,?,?)",
                    (str(identity), str(uuid4()), str(uuid4()), "a" * 64, body),
                )
            else:
                db.execute(
                    "INSERT INTO git_commits VALUES (?,?,?,?,?,?,?)",
                    (
                        str(identity),
                        str(uuid4()),
                        "refs/heads/fixture",
                        "a" * 64,
                        "prepared",
                        0,
                        body,
                    ),
                )
                db.execute(
                    "INSERT INTO git_commit_events VALUES (?,?,?,?)",
                    (str(identity), 0, "prepared", body),
                )
            additions = (root_claim, _claim(core, kind, identity))
        result = await _publish(db, binding, guard, window, additions)
        db.execute("COMMIT")
        assert result.revision == len(additions)
        assert _read(db, binding) == result


@pytest.mark.parametrize("point", ["after_business", "after_proofs"])
async def test_caller_rollback_preserves_previous_genesis_without_partial_authentication(
    tmp_path, point
):
    with _binding() as (binding, guard), _connection(tmp_path / "rollback.db") as db:
        initial = await _genesis(db, binding, guard)
        db.execute("BEGIN IMMEDIATE")
        window = begin_git_prefix_write(
            db, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
        )
        core = _link(db)
        if point == "after_proofs":
            await _publish(db, binding, guard, window, (_claim(core),))
        db.execute("ROLLBACK")
        assert _read(db, binding) == initial
        assert db.execute("SELECT count(*) FROM git_record_publications").fetchone() == (0,)


async def test_fresh_unsigned_business_rows_never_receive_genesis_backfill(tmp_path):
    with _binding() as (binding, guard), _connection(tmp_path / "unsigned.db") as db:
        db.execute("BEGIN IMMEDIATE")
        initialize_git_store_v2(db)
        _link(db)
        with pytest.raises(KernelError, match="不能升代") as caught:
            await initialize_git_prefix_genesis(
                db, binding.git, binding.git_prefix, guard, cancel=CancelToken()
            )
        assert caught.value.code == "git_delivery_store_legacy_unproven"
        assert db.execute("SELECT count(*) FROM git_prefix_anchor").fetchone() == (0,)
        db.execute("ROLLBACK")


async def test_original_cancel_survives_without_new_sqlite_commits(tmp_path):
    with _binding() as (binding, guard), _connection(tmp_path / "cancel.db") as db:
        initial = await _genesis(db, binding, guard)
        db.execute("BEGIN IMMEDIATE")
        window = begin_git_prefix_write(
            db, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
        )
        core = _link(db)
        cancel = CancelToken()
        cancel.cancel()
        with pytest.raises(TurnCancelled):
            await publish_git_prefix_changes(
                window, (_claim(core),), binding.git, binding.git_prefix, guard, cancel=cancel
            )
        db.execute("ROLLBACK")
        assert _read(db, binding) == initial


async def test_window_copy_and_stale_anchor_rejected_before_new_proof(tmp_path):
    with _binding() as (binding, guard), _connection(tmp_path / "window.db") as db:
        initial = await _genesis(db, binding, guard)
        db.execute("BEGIN IMMEDIATE")
        window = begin_git_prefix_write(
            db, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
        )
        core = _link(db)
        with pytest.raises(KernelError):
            await _publish(db, binding, guard, replace(window), (_claim(core),))
        assert db.execute("SELECT count(*) FROM git_record_publications").fetchone() == (0,)
        db.execute("UPDATE git_prefix_anchor SET revision=revision+1")
        with pytest.raises(KernelError):
            await _publish(db, binding, guard, window, (_claim(core),))
        db.execute("ROLLBACK")
        assert _read(db, binding) == initial


async def test_catalog_canonical_roundtrip_and_full_table_hashes(tmp_path):
    with _binding() as (binding, guard), _connection(tmp_path / "canonical.db") as db:
        catalog = await _genesis(db, binding, guard)
        body = encode_git_prefix_catalog(catalog)
        assert decode_git_prefix_catalog(body) == catalog
        for bad in (body + b"\n", body.replace(b'"revision":0', b'"revision":0,"revision":0')):
            with pytest.raises(KernelError):
                decode_git_prefix_catalog(bad)
        db.execute("BEGIN")
        rows = capture_git_prefix_rows(db, checkpoint=CancelToken().checkpoint)
        assert rows.tables == catalog.tables
        assert all(t.sha256 == sha256(b"").hexdigest() for t in catalog.tables if t.rows == 0)
        db.execute("ROLLBACK")


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_actual_snapshot2_record_bytes_in_new_authentication_fixture(tmp_path, object_format):
    """真实prepared T2/A/D领域模型作为新行夹具；不是默认产品Bridge端到端验收。"""
    with _source(tmp_path, depth=32, object_format=object_format) as source:
        record = source.runtime.plan_worktree(source.transaction.transaction_id, source.anchor)
        body = record.model_dump_json(warnings="error")
        assert source.store.load(source.transaction.transaction_id).state == "prepared"
        with _binding() as (binding, guard), _connection(tmp_path / "auth.db") as db:
            await _genesis(db, binding, guard)
            db.execute("BEGIN IMMEDIATE")
            window = begin_git_prefix_write(
                db, binding.git, binding.git_prefix, checkpoint=CancelToken().checkpoint
            )
            core = _link(db)
            db.execute(
                "INSERT INTO git_worktrees VALUES (?,?,?,?,?,?)",
                (
                    str(record.worktree_id),
                    str(record.plan.transaction_id),
                    record.plan.fingerprint,
                    record.state,
                    record.sequence,
                    body,
                ),
            )
            db.execute(
                "INSERT INTO git_worktree_events VALUES (?,?,?,?)",
                (str(record.worktree_id), record.sequence, record.state, body),
            )
            result = await _publish(
                db,
                binding,
                guard,
                window,
                (_claim(core), _claim(core, "worktree_event", record.worktree_id)),
            )
            db.execute("COMMIT")
            assert _read(db, binding) == result
            payload = db.execute("SELECT payload FROM git_worktrees").fetchone()[0]
            assert payload == body
            assert ManagedGitWorktreeRecord.model_validate_json(payload, strict=True) == record
