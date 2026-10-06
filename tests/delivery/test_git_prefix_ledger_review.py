"""有限五 kind 物理账本的独立对抗验证；不验收业务语义、批准或原生桥接。

正文仅为合成私有合同，不代表真实 ProductLink 模型。使用真实 SQLite v2、
原 SessionPublicationBinding 与 SecretPublicationScope，不调用模型或凭据服务。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4
from weakref import ref

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_store_genesis import initialize_git_store_v2
from harnessix.product_config.git_prefix_catalog import GIT_PREFIX_TABLES, encode_git_prefix_catalog
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_records import verify_record_streams
from harnessix.product_config.git_prefix_rows import capture_git_prefix_rows
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prefix_writer import (
    GitPrefixWriteWindow,
    begin_git_prefix_write,
    initialize_git_prefix_genesis,
    publish_git_prefix_changes,
)
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.session.store_publication import SessionPublicationBinding, extend_prefix
from tests.session.test_publication_seal import KEY, scope

pytestmark = pytest.mark.asyncio

KINDS = ("product_link", "object_inventory", "worktree_event", "checkpoint", "commit_event")
TABLES = {
    "product_link": ("git_product_links", "git_product_link_events", "route_id", "phase"),
    "object_inventory": (
        "git_object_inventories",
        "git_object_inventory_events",
        "inventory_id",
        "phase",
    ),
    "worktree_event": ("git_worktrees", "git_worktree_events", "worktree_id", "state"),
    "checkpoint": ("git_checkpoints", None, "checkpoint_id", None),
    "commit_event": ("git_commits", "git_commit_events", "commit_id", "state"),
}
UNPROVEN = "publication_history_unproven"
PRIVATE_NOTE = "仅供物理字节认证的合成私有合同"


class Ledger:
    """独立连接与合成记录；所有提交、回滚均显式由测试调用者负责。"""

    def __init__(self, path, protection):
        self.path, self.protection = path, protection
        self.store_id, self.key_id = uuid4(), uuid4()
        self.binding = SessionPublicationBinding(self.store_id, self.key_id, KEY, protection)
        self.sql_cancel = CancelToken()
        self._connection_lifetime = None
        self.db = self.connect()
        self.db.execute("BEGIN")
        initialize_git_store_v2(self.db)
        self.owner = {
            name: uuid4() for name in ("delivery_id", "thread_id", "turn_id", "call_id", "route_id")
        }
        self.claims = {}
        self.baseline = None

    def connect(self, *, readonly=False):
        assert self._connection_lifetime is None, "旧 SQL window 必须先退出"
        target = f"{self.path.as_uri()}?mode=ro" if readonly else str(self.path)
        db = sqlite3.connect(target, uri=readonly, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        self._connection_lifetime = ExitStack()
        self._connection_lifetime.enter_context(
            git_prefix_sql_window(db, checkpoint=self.sql_cancel.checkpoint)
        )
        return db

    def close_database(self):
        """保持原 SQL 检查点到连接生命期末；退出窗口之后才关闭连接。"""
        try:
            self._connection_lifetime.close()
        finally:
            self._connection_lifetime = None
            self.db.close()

    def snapshot(self):
        return tuple(
            (table, tuple(self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")))
            for table in (*GIT_PREFIX_TABLES, "git_prefix_anchor")
        )

    def commit(self):
        assert self.db.in_transaction
        self.db.execute("COMMIT")
        assert not self.db.in_transaction
        self.baseline = self.snapshot()

    def read(self, *, binding=None, checkpoint=None):
        binding = binding or self.binding
        return read_git_prefix_catalog(
            self.db,
            binding.git_verifier,
            binding.git_prefix_verifier,
            checkpoint=checkpoint or CancelToken().checkpoint,
        )

    def begin(self):
        self.db.execute("BEGIN")
        return begin_git_prefix_write(
            self.db,
            self.binding.git,
            self.binding.git_prefix,
            checkpoint=CancelToken().checkpoint,
        )

    async def genesis(self):
        result = await initialize_git_prefix_genesis(
            self.db,
            self.binding.git,
            self.binding.git_prefix,
            self.protection,
            cancel=CancelToken(),
        )
        assert result.revision == 0
        self.commit()
        return result

    def candidate(self, kind, *, prior=None, new_owner=False):
        owner = {name: uuid4() for name in self.owner} if new_owner else self.owner
        values = dict(
            owner,
            record_kind=kind,
            record_id=owner["route_id"] if kind == "product_link" else uuid4(),
            publication_epoch=uuid4(),
            sequence=1,
            previous_sha256="0" * 64,
        )
        if prior is not None:
            row = self.db.execute(
                "SELECT prefix_sha256 FROM git_record_publications "
                "WHERE record_kind=? AND record_id=? AND sequence=?",
                (kind, str(prior.record_id), prior.sequence),
            ).fetchone()
            values = prior.model_dump()
            values.update(sequence=prior.sequence + 1, previous_sha256=row[0])
        return GitDeliveryRecordClaims(**values)

    def insert(self, claims, *, padding=""):
        kind, identity, sequence = claims.record_kind, str(claims.record_id), claims.sequence - 1
        body = json.dumps(
            {
                "spec_version": "review.synthetic-private/v1",
                "kind": kind,
                "record_id": identity,
                "sequence": sequence,
                "private_note": PRIVATE_NOTE,
                "padding": padding,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        current, events, identity_column, phase_column = TABLES[kind]
        phase = "materials_ready" if kind == "object_inventory" else "prepared"
        owner = tuple(str(getattr(claims, name)) for name in self.owner)
        rows = {
            "object_inventory": (
                identity,
                owner[0],
                owner[4],
                phase,
                sequence,
                "a" * 64,
                "b" * 64,
                body,
            ),
            "worktree_event": (identity, str(uuid4()), "c" * 64, phase, sequence, body),
            "checkpoint": (identity, str(uuid4()), str(uuid4()), "d" * 64, body),
            "commit_event": (
                identity,
                str(uuid4()),
                f"refs/heads/review-{identity}",
                "e" * 64,
                phase,
                sequence,
                body,
            ),
        }
        rows["product_link"] = (
            identity,
            *owner[:4],
            "checkpoint",
            "a" * 64,
            "b" * 64,
            phase,
            sequence,
            body,
        )
        if sequence == 0:
            values = rows[kind]
            self.db.execute(
                f"INSERT INTO {current} VALUES ({','.join('?' for _ in values)})", values
            )
        else:
            self.db.execute(
                f"UPDATE {current} SET sequence=?, {phase_column}=?, payload=? "
                f"WHERE {identity_column}=?",
                (sequence, phase, body, identity),
            )
        if events is not None:
            self.db.execute(
                f"INSERT INTO {events} VALUES (?,?,?,?)", (identity, sequence, phase, body)
            )
        return body.encode()

    async def publish(self, window, additions, *, cancel=None):
        return await publish_git_prefix_changes(
            window,
            tuple(additions),
            self.binding.git,
            self.binding.git_prefix,
            self.protection,
            cancel=cancel or CancelToken(),
        )

    async def seed(self):
        await self.genesis()
        window = self.begin()
        additions = []
        for kind in KINDS:
            claims = self.candidate(kind)
            self.insert(claims)
            self.claims[kind] = claims
            additions.append(claims)
        catalog = await self.publish(window, additions)
        assert catalog.revision == 5
        assert self.db.execute("PRAGMA foreign_key_check").fetchall() == []
        self.commit()
        return catalog

    def insert_publication(self, claims, body, seal):
        values = (
            claims.record_kind,
            str(claims.record_id),
            str(claims.publication_epoch),
            claims.sequence,
            *(str(getattr(claims, name)) for name in self.owner),
            claims.previous_sha256,
            sha256(body).hexdigest(),
            len(body),
            extend_prefix(claims.previous_sha256, seal),
            seal,
        )
        self.db.execute(
            "INSERT INTO git_record_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )

    def bridge(self):
        self.db.execute(
            "INSERT INTO git_native_bridge_index VALUES (?,?,?,?,?,?)",
            (
                str(uuid4()),
                str(uuid4()),
                str(self.owner["route_id"]),
                str(uuid4()),
                str(uuid4()),
                "f" * 64,
            ),
        )


@pytest.fixture
def ledger(tmp_path):
    with scope() as protection:
        ledger = Ledger(tmp_path / "review.sqlite3", protection)
        try:
            yield ledger
        finally:
            ledger.db.rollback()
            ledger.close_database()
            ledger.binding.close()


@contextmanager
def rejected(ledger, *, code=UNPROVEN, exception=KernelError):
    """即使发现漏拒绝，也由调用者回滚并验证旧有效锚及全部旧物理行。"""
    was_in_transaction = ledger.db.in_transaction
    try:
        with pytest.raises(exception) as caught:
            yield
        if code is not None:
            assert caught.value.code == code
        assert PRIVATE_NOTE not in str(caught.value)
        assert ledger.db.in_transaction == was_in_transaction, "原语不得代调用者提交或回滚"
    finally:
        ledger.db.rollback()
        assert ledger.snapshot() == ledger.baseline
        ledger.db.execute("BEGIN")
        try:
            ledger.read()
        finally:
            ledger.db.rollback()


async def test_real_commit_reopen_new_scope_and_verify_only(ledger, monkeypatch):
    expected = await ledger.seed()
    ledger.close_database()
    ledger.db = ledger.connect()
    with scope("review-current-scope-material", "10") as current:
        binding = SessionPublicationBinding(ledger.store_id, ledger.key_id, KEY, current)
        try:

            def no_issue(*args, **kwargs):
                pytest.fail("只验真路径不得签发、访问当前 Scope 或补写")

            monkeypatch.setattr(binding.git, "issue", no_issue)
            monkeypatch.setattr(binding.git_prefix, "issue", no_issue)
            monkeypatch.setattr(current, "publication_context", no_issue)
            monkeypatch.setattr(current, "assert_public_json", no_issue)
            before, changes = ledger.snapshot(), ledger.db.total_changes
            ledger.db.execute("BEGIN")
            actual = ledger.read(binding=binding)
            assert encode_git_prefix_catalog(actual) == encode_git_prefix_catalog(expected)
            assert {s.first.record_kind for s in actual.streams} == set(KINDS)
            assert ledger.snapshot() == before and ledger.db.total_changes == changes
            ledger.db.rollback()
        finally:
            binding.close()


@pytest.mark.parametrize("wrong", ["store", "key_id", "key"])
async def test_other_binding_cannot_authenticate_committed_database(ledger, wrong):
    await ledger.seed()
    with scope() as protection:
        other = SessionPublicationBinding(
            uuid4() if wrong == "store" else ledger.store_id,
            uuid4() if wrong == "key_id" else ledger.key_id,
            bytes(reversed(KEY)) if wrong == "key" else KEY,
            protection,
        )
        try:
            ledger.db.execute("BEGIN")
            with rejected(ledger):
                ledger.read(binding=other)
        finally:
            other.close()


@pytest.mark.parametrize("kind", KINDS)
async def test_delete_complete_tail_entity_keeps_seals_but_old_anchor_rejects(ledger, kind):
    await ledger.seed()
    window = ledger.begin()
    tail = ledger.candidate(kind, new_owner=kind == "product_link")
    ledger.insert(tail)
    await ledger.publish(window, (tail,))
    ledger.commit()
    ledger.db.execute("BEGIN")
    with rejected(ledger):
        current, events, identity, _ = TABLES[kind]
        if events:
            ledger.db.execute(f"DELETE FROM {events} WHERE {identity}=?", (str(tail.record_id),))
        ledger.db.execute(
            "DELETE FROM git_record_publications WHERE record_id=?", (str(tail.record_id),)
        )
        ledger.db.execute(f"DELETE FROM {current} WHERE {identity}=?", (str(tail.record_id),))
        assert ledger.db.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = capture_git_prefix_rows(ledger.db, checkpoint=CancelToken().checkpoint)
        streams = verify_record_streams(
            rows, ledger.binding.git_verifier, checkpoint=CancelToken().checkpoint
        )
        assert sum(s.count for s in streams) == 5, "剩余单条原 MAC 与连续前缀仍合法"
        assert ledger.db.execute("SELECT revision FROM git_prefix_anchor").fetchone() == (6,)
        ledger.read()


@pytest.mark.parametrize("orphan", ["event", "publication", "second_epoch"])
async def test_orphans_and_individually_valid_second_epoch_are_rejected(ledger, orphan):
    await ledger.seed()
    ledger.db.execute("BEGIN")
    with rejected(ledger):
        if orphan == "event":
            claims = ledger.candidate("object_inventory")
            ledger.insert(claims)
        else:
            prior = ledger.claims["product_link"]
            claims = (
                GitDeliveryRecordClaims(**(prior.model_dump() | {"publication_epoch": uuid4()}))
                if orphan == "second_epoch"
                else ledger.candidate("object_inventory")
            )
            body = (
                ledger.db.execute("SELECT payload FROM git_product_links").fetchone()[0].encode()
                if orphan == "second_epoch"
                else b'{"private":"orphan"}'
            )
            seal = await ledger.binding.git.issue(
                claims, body, ledger.protection, cancel=CancelToken()
            )
            ledger.binding.git_verifier.verify(
                seal, claims, body, checkpoint=CancelToken().checkpoint
            )
            ledger.insert_publication(claims, body, seal)
        rows = capture_git_prefix_rows(ledger.db, checkpoint=CancelToken().checkpoint)
        verify_record_streams(
            rows, ledger.binding.git_verifier, checkpoint=CancelToken().checkpoint
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "old_phase",
        "mac",
        "last_payload",
        "current_index",
        "publication_index",
        "native_index",
        "anchor_mac",
    ],
)
async def test_committed_anchor_rejects_all_physical_tampering(ledger, mutation):
    await ledger.seed()
    window = ledger.begin()
    next_claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(next_claims)
    await ledger.publish(window, (next_claims,))
    ledger.commit()
    ledger.db.execute("BEGIN")
    with rejected(ledger):
        if mutation == "old_phase":
            ledger.db.execute("UPDATE git_product_link_events SET phase='failed' WHERE sequence=0")
        elif mutation == "last_payload":
            ledger.db.execute(
                "UPDATE git_product_link_events SET payload=payload||' ' WHERE sequence=1"
            )
            ledger.db.execute("UPDATE git_product_links SET payload=payload||' '")
        elif mutation == "current_index":
            ledger.db.execute("UPDATE git_product_links SET route_fingerprint=?", ("c" * 64,))
        elif mutation == "publication_index":
            ledger.db.execute(
                "UPDATE git_record_publications SET body_sha256=? WHERE record_kind='checkpoint'",
                ("c" * 64,),
            )
        elif mutation == "native_index":
            ledger.bridge()
        else:
            table = "git_prefix_anchor" if mutation == "anchor_mac" else "git_record_publications"
            rowid, raw = ledger.db.execute(f"SELECT rowid,seal FROM {table} LIMIT 1").fetchone()
            value = json.loads(raw)
            value["tag"] = "0" * 64 if value["tag"] != "0" * 64 else "1" * 64
            ledger.db.execute(
                f"UPDATE {table} SET seal=? WHERE rowid=?",
                (json.dumps(value, separators=(",", ":")).encode(), rowid),
            )
        ledger.read()


async def test_writer_cannot_resign_changed_old_redundant_phase(ledger):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    ledger.db.execute("UPDATE git_product_link_events SET phase='failed' WHERE sequence=0")
    with rejected(ledger):
        await ledger.publish(window, (claims,))


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind != "checkpoint"])
async def test_current_phase_must_equal_last_event_phase_before_issue(ledger, kind, monkeypatch):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate("object_inventory")
    ledger.insert(claims)
    current, _, identity, phase_column = TABLES[kind]
    phase = "effect_closed" if kind == "object_inventory" else "failed"
    ledger.db.execute(
        f"UPDATE {current} SET {phase_column}=? WHERE {identity}=?",
        (phase, str(ledger.claims[kind].record_id)),
    )

    def no_issue(*args, **kwargs):
        pytest.fail("current 与最后事件 phase 不一致时必须先拒绝，不能新签")

    monkeypatch.setattr(ledger.binding.git, "issue", no_issue)
    with rejected(ledger):
        await ledger.publish(window, (claims,))


@pytest.mark.parametrize(
    "kind,column",
    [
        ("product_link", "action_kind"),
        ("product_link", "core_sha256"),
        ("product_link", "route_fingerprint"),
        ("object_inventory", "scope_sha256"),
        ("object_inventory", "inventory_sha256"),
        ("worktree_event", "transaction_id"),
        ("worktree_event", "plan_fingerprint"),
        ("commit_event", "checkpoint_id"),
        ("commit_event", "branch_ref"),
        ("commit_event", "spec_fingerprint"),
    ],
)
async def test_another_entity_append_cannot_launder_current_immutable_binding(
    ledger, kind, column, monkeypatch
):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate("object_inventory")
    ledger.insert(claims)
    current, _, identity, _ = TABLES[kind]
    replacement = (
        "commit"
        if column == "action_kind"
        else str(uuid4())
        if column.endswith("_id")
        else "refs/heads/review-tampered"
        if column == "branch_ref"
        else "9" * 64
    )
    ledger.db.execute(
        f"UPDATE {current} SET {column}=? WHERE {identity}=?",
        (replacement, str(ledger.claims[kind].record_id)),
    )
    assert ledger.db.execute("PRAGMA foreign_key_check").fetchall() == []

    def no_issue(*args, **kwargs):
        pytest.fail("旧 current 不可变绑定列被改写时必须在新签之前拒绝")

    monkeypatch.setattr(ledger.binding.git, "issue", no_issue)
    with rejected(ledger):
        await ledger.publish(window, (claims,))


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind != "checkpoint"])
async def test_matching_current_and_last_event_phase_can_advance(ledger, kind):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate(kind, prior=ledger.claims[kind])
    ledger.insert(claims)
    current, events, identity, phase_column = TABLES[kind]
    phase = "effect_closed" if kind == "object_inventory" else "approved"
    ledger.db.execute(
        f"UPDATE {current} SET {phase_column}=? WHERE {identity}=?", (phase, str(claims.record_id))
    )
    ledger.db.execute(
        f"UPDATE {events} SET {phase_column}=? WHERE {identity}=? AND sequence=?",
        (phase, str(claims.record_id), claims.sequence - 1),
    )
    catalog = await ledger.publish(window, (claims,))
    assert catalog.revision == 6
    ledger.commit()
    ledger.close_database()
    ledger.db = ledger.connect()
    ledger.db.execute("BEGIN")
    assert ledger.read() == catalog
    ledger.db.rollback()


@pytest.mark.parametrize("stage", ["record", "prefix"])
@pytest.mark.parametrize(
    "mutation", ["old_phase", "last_payload", "native_index", "anchor", "savepoint"]
)
async def test_real_protection_callback_mutation_inside_await_rejects(
    ledger, monkeypatch, stage, mutation
):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    original = ledger.protection.assert_public_json
    reached = []

    def mutate(value, *, checkpoint):
        original(value, checkpoint=checkpoint)
        actual = "record" if "record_kind" in value["claims"] else "prefix"
        if actual == stage and not reached:
            reached.append(actual)
            if mutation == "old_phase":
                ledger.db.execute(
                    "UPDATE git_product_link_events SET phase='failed' WHERE sequence=0"
                )
            elif mutation == "last_payload":
                ledger.db.execute(
                    "UPDATE git_product_link_events SET payload=payload||' ' WHERE sequence=1"
                )
                ledger.db.execute("UPDATE git_product_links SET payload=payload||' '")
            elif mutation == "native_index":
                ledger.bridge()
            elif mutation == "savepoint":
                ledger.db.execute("/* 合成回调事务边界 */ SAVEPOINT review_guard")
                ledger.db.execute("-- 合成回调事务边界\n RELEASE review_guard")
            else:
                ledger.db.execute("UPDATE git_prefix_anchor SET revision=revision+1")

    monkeypatch.setattr(ledger.protection, "assert_public_json", mutate)
    with rejected(ledger):
        await ledger.publish(window, (claims,))
    assert reached == [stage], "必须实际进入原保护回调，而非预检查偶然拒绝"


@pytest.mark.parametrize("failure", ["token", "parent", "protection"])
@pytest.mark.parametrize("stage", ["record", "prefix"])
async def test_cancel_or_guard_failure_after_mutation_rolls_back_to_old_anchor(
    ledger, monkeypatch, failure, stage
):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    token, reached = CancelToken(), []
    original = ledger.protection.assert_public_json

    def interrupt(value, *, checkpoint):
        original(value, checkpoint=checkpoint)
        actual = "record" if "record_kind" in value["claims"] else "prefix"
        if actual == stage and not reached:
            reached.append(actual)
            ledger.db.execute("UPDATE git_product_link_events SET phase='failed' WHERE sequence=0")
            if failure == "token":
                token.cancel()
            elif failure == "parent":
                asyncio.current_task().cancel()
            else:
                raise ValueError(PRIVATE_NOTE)

    monkeypatch.setattr(ledger.protection, "assert_public_json", interrupt)
    exception = {
        "token": TurnCancelled,
        "parent": asyncio.CancelledError,
        "protection": KernelError,
    }[failure]
    with rejected(
        ledger,
        exception=exception,
        code="public_output_protection_failed" if failure == "protection" else None,
    ):
        await asyncio.create_task(ledger.publish(window, (claims,), cancel=token))
    assert reached == [stage]


@pytest.mark.parametrize("entry", ["capture", "read", "begin", "genesis", "initialize"])
async def test_no_transaction_is_not_implicitly_opened(ledger, entry):
    await ledger.genesis()
    with pytest.raises(KernelError) as caught:
        if entry == "capture":
            capture_git_prefix_rows(ledger.db, checkpoint=CancelToken().checkpoint)
        elif entry == "read":
            ledger.read()
        elif entry == "begin":
            begin_git_prefix_write(
                ledger.db,
                ledger.binding.git,
                ledger.binding.git_prefix,
                checkpoint=CancelToken().checkpoint,
            )
        elif entry == "genesis":
            await initialize_git_prefix_genesis(
                ledger.db,
                ledger.binding.git,
                ledger.binding.git_prefix,
                ledger.protection,
                cancel=CancelToken(),
            )
        else:
            initialize_git_store_v2(ledger.db)
    assert caught.value.code == "git_delivery_store_transaction_required"
    assert not ledger.db.in_transaction and ledger.snapshot() == ledger.baseline


async def test_readonly_uri_verify_only_is_legal(ledger):
    expected = await ledger.seed()
    ledger.close_database()
    ledger.db = ledger.connect(readonly=True)
    ledger.db.execute("BEGIN")
    assert ledger.read() == expected
    assert ledger.db.total_changes == 0
    ledger.db.rollback()


@pytest.mark.parametrize("mode", ["query_only", "uri"])
@pytest.mark.parametrize("entry", ["begin", "genesis", "initialize"])
async def test_readonly_writers_reject_before_issuing_or_writing(ledger, mode, entry):
    await ledger.genesis()
    if mode == "uri":
        ledger.close_database()
        ledger.db = ledger.connect(readonly=True)
    else:
        ledger.db.execute("PRAGMA query_only=ON")
    ledger.db.execute("BEGIN")
    with rejected(ledger, code="git_delivery_store_read_only"):
        if entry == "begin":
            begin_git_prefix_write(
                ledger.db,
                ledger.binding.git,
                ledger.binding.git_prefix,
                checkpoint=CancelToken().checkpoint,
            )
        elif entry == "genesis":
            await initialize_git_prefix_genesis(
                ledger.db,
                ledger.binding.git,
                ledger.binding.git_prefix,
                ledger.protection,
                cancel=CancelToken(),
            )
        else:
            initialize_git_store_v2(ledger.db)


@pytest.mark.parametrize(
    "forgery",
    [
        "copy",
        "replace",
        "construct",
        "witness_transplant",
        "constructed_self_witness",
        "constructed_weakref_witness",
    ],
)
async def test_copied_or_constructed_window_has_no_issuer_capability(ledger, forgery):
    await ledger.seed()
    original = ledger.begin()
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    if forgery == "copy":
        forged = copy.copy(original)
    elif forgery == "replace":
        forged = replace(original)
    else:
        witness = original._witness if forgery == "witness_transplant" else None
        if forgery == "constructed_self_witness":

            def self_witness():
                return forged

            witness = self_witness
        forged = GitPrefixWriteWindow(
            original.database,
            original.catalog,
            original.rows,
            original.anchor,
            original.transaction_epoch,
            _witness=witness,
        )
        if forgery == "constructed_weakref_witness":
            object.__setattr__(forged, "_witness", ref(forged))
    assert forged is not original
    with rejected(ledger):
        await ledger.publish(forged, (claims,))


@pytest.mark.parametrize(
    "boundary",
    [
        "COMMIT",
        "ROLLBACK",
        "/* 前导块注释 */ COMMIT",
        "-- 前导行注释\n /* 块注释 */ ROLLBACK",
        ";COMMIT",
        "\ufeffCOMMIT",
        "/* leading */ ; COMMIT",
    ],
)
async def test_unused_window_cannot_cross_transaction_boundary(ledger, boundary):
    await ledger.seed()
    window = ledger.begin()
    ledger.db.execute(boundary)
    ledger.db.execute("BEGIN")
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    with rejected(ledger):
        await ledger.publish(window, (claims,))


@pytest.mark.parametrize(
    "statements",
    [
        ("SAVEPOINT review_epoch", "RELEASE review_epoch"),
        (
            "/* 注释 */ SAVEPOINT review_epoch",
            "-- 注释\n ROLLBACK TO review_epoch",
            "RELEASE review_epoch",
        ),
    ],
)
async def test_unused_window_cannot_cross_savepoint_boundaries(ledger, statements):
    await ledger.seed()
    window = ledger.begin()
    for statement in statements:
        ledger.db.execute(statement)
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    with rejected(ledger):
        await ledger.publish(window, (claims,))


async def test_unused_window_cannot_cross_sql_control_context_instance(ledger):
    await ledger.seed()
    window = ledger.begin()
    ledger._connection_lifetime.close()
    ledger._connection_lifetime = ExitStack()
    ledger._connection_lifetime.enter_context(
        git_prefix_sql_window(ledger.db, checkpoint=ledger.sql_cancel.checkpoint)
    )
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    with rejected(ledger):
        await ledger.publish(window, (claims,))


async def test_used_window_cannot_publish_twice(ledger):
    await ledger.seed()
    window = ledger.begin()
    claims = ledger.candidate("product_link", prior=ledger.claims["product_link"])
    ledger.insert(claims)
    await ledger.publish(window, (claims,))
    ledger.commit()
    ledger.db.execute("BEGIN")
    next_claims = ledger.candidate("product_link", prior=claims)
    ledger.insert(next_claims)
    with rejected(ledger):
        await ledger.publish(window, (next_claims,))


async def test_publish_window_without_active_transaction_rejects(ledger):
    await ledger.seed()
    window = ledger.begin()
    ledger.db.execute("COMMIT")
    with rejected(ledger, code="git_delivery_store_transaction_required"):
        await ledger.publish(window, (ledger.claims["product_link"],))


async def test_genesis_on_authenticated_writable_database_only_verifies(ledger, monkeypatch):
    expected = await ledger.seed()
    ledger.db.execute("BEGIN")
    before, changes = ledger.snapshot(), ledger.db.total_changes

    def no_issue(*args, **kwargs):
        pytest.fail("已有有效锚只能验真，不能重签")

    monkeypatch.setattr(ledger.binding.git, "issue", no_issue)
    monkeypatch.setattr(ledger.binding.git_prefix, "issue", no_issue)
    actual = await initialize_git_prefix_genesis(
        ledger.db,
        ledger.binding.git,
        ledger.binding.git_prefix,
        ledger.protection,
        cancel=CancelToken(),
    )
    assert actual == expected
    assert ledger.snapshot() == before and ledger.db.total_changes == changes
    ledger.db.rollback()


async def test_unanchored_nonempty_v2_is_never_bulk_signed_as_genesis(ledger):
    ledger.commit()
    ledger.db.execute("BEGIN")
    ledger.insert(ledger.candidate("product_link"))
    try:
        with pytest.raises(KernelError) as caught:
            await initialize_git_prefix_genesis(
                ledger.db,
                ledger.binding.git,
                ledger.binding.git_prefix,
                ledger.protection,
                cancel=CancelToken(),
            )
        assert caught.value.code == "git_delivery_store_legacy_unproven"
        assert ledger.db.in_transaction
        assert ledger.db.execute("SELECT count(*) FROM git_product_links").fetchone() == (1,)
        assert ledger.db.execute("SELECT * FROM git_prefix_anchor").fetchall() == []
    finally:
        ledger.db.rollback()
    assert ledger.snapshot() == ledger.baseline


async def test_genesis_rejects_temp_anchor_shadow_before_unqualified_query(ledger):
    await ledger.genesis()
    ledger.db.execute("BEGIN")
    ledger.db.execute("CREATE TEMP TABLE git_prefix_anchor (private_body TEXT)")
    with rejected(ledger, code="git_delivery_store_corrupt"):
        await initialize_git_prefix_genesis(
            ledger.db,
            ledger.binding.git,
            ledger.binding.git_prefix,
            ledger.protection,
            cancel=CancelToken(),
        )


@pytest.mark.parametrize("size", [512 * 1024 + 1, 32 * 1024 * 1024 + 1])
async def test_sqlite_oversized_payload_rejects_without_raising_record_cap(ledger, size):
    await ledger.genesis()
    ledger.db.execute("BEGIN")
    claims = ledger.candidate("product_link")
    ledger.insert(claims)
    body = "x" * size
    ledger.db.execute("UPDATE git_product_links SET payload=?", (body,))
    ledger.db.execute("UPDATE git_product_link_events SET payload=?", (body,))
    with rejected(ledger, code="git_prefix_catalog_limit"):
        capture_git_prefix_rows(ledger.db, checkpoint=CancelToken().checkpoint)


async def test_exact_512kib_utf8_cell_is_not_rejected_by_smaller_cap(ledger):
    await ledger.genesis()
    ledger.db.execute("BEGIN")
    ledger.insert(ledger.candidate("product_link"))
    body = "界" * (512 * 1024 // 3) + "x" * (512 * 1024 % 3)
    assert len(body.encode()) == 512 * 1024
    ledger.db.execute("UPDATE git_product_links SET payload=?", (body,))
    ledger.db.execute("UPDATE git_product_link_events SET payload=?", (body,))
    rows = capture_git_prefix_rows(ledger.db, checkpoint=CancelToken().checkpoint)
    assert rows.table("git_product_link_events")[0][-1] == body
    ledger.db.rollback()
    assert ledger.snapshot() == ledger.baseline


async def test_aggregate_capture_above_32mib_rejects_with_each_cell_below_512kib(ledger):
    await ledger.genesis()
    ledger.db.execute("BEGIN")
    body = "x" * (256 * 1024)
    for _ in range(65):
        identity = str(uuid4())
        ledger.db.execute(
            "INSERT INTO git_worktrees VALUES (?,?,?,?,?,?)",
            (identity, str(uuid4()), "a" * 64, "prepared", 0, body),
        )
        ledger.db.execute(
            "INSERT INTO git_worktree_events VALUES (?,?,?,?)", (identity, 0, "prepared", body)
        )
    sizes = ledger.db.execute(
        "SELECT max(length(CAST(payload AS BLOB))), "
        "sum(length(CAST(payload AS BLOB))) FROM git_worktrees"
    ).fetchone()
    assert sizes[0] < 512 * 1024 and sizes[1] * 2 > 32 * 1024 * 1024
    with rejected(ledger, code="git_prefix_catalog_limit"):
        capture_git_prefix_rows(ledger.db, checkpoint=CancelToken().checkpoint)


@pytest.mark.parametrize("entry", ["capture", "read", "begin", "genesis"])
async def test_raw_connection_without_host_sql_window_is_rejected(ledger, entry):
    await ledger.genesis()
    raw = sqlite3.connect(ledger.path, isolation_level=None)
    try:
        raw.execute("BEGIN")
        with pytest.raises(KernelError) as caught:
            if entry == "capture":
                capture_git_prefix_rows(raw, checkpoint=CancelToken().checkpoint)
            elif entry == "read":
                read_git_prefix_catalog(
                    raw,
                    ledger.binding.git_verifier,
                    ledger.binding.git_prefix_verifier,
                    checkpoint=CancelToken().checkpoint,
                )
            elif entry == "begin":
                begin_git_prefix_write(
                    raw,
                    ledger.binding.git,
                    ledger.binding.git_prefix,
                    checkpoint=CancelToken().checkpoint,
                )
            else:
                await initialize_git_prefix_genesis(
                    raw,
                    ledger.binding.git,
                    ledger.binding.git_prefix,
                    ledger.protection,
                    cancel=CancelToken(),
                )
        assert caught.value.code == "git_prefix_sql_control_required"
        assert raw.in_transaction
        raw.rollback()
        assert ledger.snapshot() == ledger.baseline
    finally:
        raw.close()


@pytest.mark.parametrize("host_failure", ["cancel", "owner", "deadline"])
async def test_sql_interruption_preserves_first_original_host_exception(ledger, host_failure):
    await ledger.genesis()
    raw = sqlite3.connect(ledger.path, isolation_level=None)
    token, calls, original_errors = CancelToken(), [], []
    expected_type = TurnCancelled if host_failure == "cancel" else KernelError

    def host_checkpoint():
        calls.append(1)
        if len(calls) < 3:
            token.checkpoint()
            return
        try:
            if host_failure == "cancel":
                token.cancel()
                token.checkpoint()
            # 仅模拟宿主已有检查点的原异常，不创建 Owner 或新的期限。
            raise KernelError(f"review_original_{host_failure}", "合成宿主检查点已撤销")
        except BaseException as error:
            original_errors.append(error)
            raise

    try:
        raw.execute("BEGIN")
        with pytest.raises(expected_type) as caught:
            with git_prefix_sql_window(raw, checkpoint=host_checkpoint):
                raw.execute(
                    "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL "
                    "SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n"
                ).fetchone()
        assert len(calls) >= 3 and original_errors
        assert caught.value is original_errors[0], "SQLITE_INTERRUPT 不得遮蔽首次原异常"
        assert raw.in_transaction
        raw.rollback()
        assert ledger.snapshot() == ledger.baseline
    finally:
        raw.close()


async def test_nested_sql_window_does_not_replace_owned_handler(ledger):
    await ledger.genesis()
    with pytest.raises(KernelError) as caught:
        with git_prefix_sql_window(ledger.db, checkpoint=CancelToken().checkpoint):
            pytest.fail("同一专用连接不得嵌套安装 SQL handler")
    assert caught.value.code == "git_prefix_sql_control_conflict"
    ledger.db.execute("BEGIN")
    assert ledger.read().revision == 0
    ledger.db.rollback()
