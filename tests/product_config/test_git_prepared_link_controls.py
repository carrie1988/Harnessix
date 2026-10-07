"""真实 SDK 待审批关联的失败回归；synthetic 注入不代表业务 Git 写入。"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from contextlib import closing, contextmanager
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_prepared_link_rows import prepared_link_columns
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.sqlite_readonly import readonly_database
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.product_config.git_repository_observation_support import _source_snapshot
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _ledger,
    _publish_raw,
    _rows,
)


def _business_state(actual):
    """全表核对原 Session/批准/Route/CAS 账本，并保留原 U 的物理快照。"""
    scenario = actual.scenario
    with closing(readonly_database(scenario.session.path)) as session:
        session_rows = _rows(session)
    stores = (scenario.router._audit, scenario.router._plans, scenario.transactions)
    return (
        _source_snapshot(scenario.root),
        session_rows,
        tuple((_rows(store._db), store._db.total_changes) for store in stores),
        scenario.router.status(actual.route.plan.execution.plan_id),
        len(scenario.bundle.requests),
    )


@contextmanager
def _business_unchanged(actual):
    before = _business_state(actual)
    try:
        yield
    finally:
        assert _business_state(actual) == before
        assert not list(actual.preparer.worktree_parent.iterdir())


async def _prepare(actual, ledger):
    return await ledger.prepare(
        actual.route.plan.execution.plan_id, cancel=CancelToken(), checkpoint=lambda: None
    )


async def _reject(actual, ledger, db, operation, *, codes=None):
    """调用方已经开启事务；拒绝不能写表或自行处理调用方事务。"""
    before, changes = _rows(db), db.total_changes
    assert db.in_transaction
    with pytest.raises(KernelError) as caught:
        if operation == "read":
            await ledger.read_all(cancel=CancelToken(), checkpoint=lambda: None)
        else:
            assert operation == "prepare"
            await _prepare(actual, ledger)
    if codes is not None:
        assert caught.value.code in codes
    assert db.in_transaction
    assert _rows(db) == before
    assert db.total_changes == changes
    return caught.value


def _persisted(actual, expected):
    """独立只读连接复核 caller rollback 后没有持久 partial link。"""
    with _database(actual, read_only=True) as ro:
        ro.execute("BEGIN")
        assert _rows(ro) == expected
        assert ro.total_changes == 0


async def _sealed(actual, db):
    await _genesis(actual, db)
    db.execute("BEGIN IMMEDIATE")
    link = await _prepare(actual, _ledger(actual, db))
    assert link.phase == "prepared" and link.approval.decision is None
    db.execute("COMMIT")
    return link, _rows(db)


async def test_real_host_mismatches_reject_without_writing_original_business(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        scenario = actual.scenario
        foreign = scenario.state / "foreign-host"
        with (
            _business_unchanged(actual),
            closing(
                SQLiteWorkspaceTransactionStore(foreign / "workspace-transactions")
            ) as transactions,
            closing(SQLiteActionAuditStore(foreign / "action-audit.db")) as audit,
            closing(SQLiteExecutionPlanStore(foreign / "execution-plans.db")) as plans,
        ):
            session = SQLiteSessionStore(foreign / "sessions.db")
            await session.initialize()
            artifacts = SQLiteArtifactStore(session)
            reader = GitReadRuntime(
                scenario.root,
                scenario.reader._executable,
                state_directory=foreign / "git-read",
                output_redaction=scenario.reader._output_redaction,
            )
            with _database(actual) as db:
                _link, sealed = await _sealed(actual, db)
                ledger = _ledger(actual, db)
                foreign_scope = "0" * 64 if ledger._workspace_scope != "0" * 64 else "1" * 64
                mismatches = (
                    (ledger, "_core_store", ProductGitDeliveryCoreStore(transactions)),
                    (ledger, "_artifacts", artifacts),
                    (ledger, "_reader", reader),
                    (
                        ledger,
                        "_ports",
                        WorkspaceSnapshotPorts(transactions.put_blob, transactions.blob),
                    ),
                    (ledger, "_workspace_scope", foreign_scope),
                    (scenario.router, "_audit", audit),
                    (scenario.router, "_plans", plans),
                )
                for target, name, replacement in mismatches:
                    for operation in ("read", "prepare"):
                        db.execute("BEGIN IMMEDIATE")
                        with monkeypatch.context() as patch:
                            patch.setattr(target, name, replacement)
                            await _reject(actual, ledger, db, operation)
                        db.execute("ROLLBACK")
                        _persisted(actual, sealed)

                # 真正的另一物理 DB 连接也不能借同一 Session 进入原固定账本。
                with _database(actual, path=foreign / "git-delivery.db") as other:
                    db.backup(other)
                    other_rows = _rows(other)
                    for operation in ("read", "prepare"):
                        other.execute("BEGIN IMMEDIATE")
                        await _reject(
                            actual,
                            _ledger(actual, other),
                            other,
                            operation,
                            codes={"git_prepared_link_host_invalid"},
                        )
                        other.execute("ROLLBACK")
                        assert _rows(other) == other_rows
                        _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


@contextmanager
def _artifact_damage(actual, artifact_id, column, value):
    """物理故障注入：只损坏再恢复原 Artifact 行，不重签 MAC 或审批。"""
    assert column in {"body", "publication_seal", "state", "workspace_scope"}
    with closing(sqlite3.connect(actual.scenario.session.path, isolation_level=None)) as session:
        previous, body = session.execute(
            f"SELECT {column},body FROM agent_artifacts WHERE artifact_id=?", (str(artifact_id),)
        ).fetchone()
        if column == "state":
            session.execute(
                "UPDATE agent_artifacts SET state=?,body=NULL WHERE artifact_id=?",
                (value, str(artifact_id)),
            )
        else:
            session.execute(
                f"UPDATE agent_artifacts SET {column}=? WHERE artifact_id=?",
                (value, str(artifact_id)),
            )
        try:
            yield
        finally:
            if column == "state":
                session.execute(
                    "UPDATE agent_artifacts SET state=?,body=? WHERE artifact_id=?",
                    (previous, body, str(artifact_id)),
                )
            else:
                session.execute(
                    f"UPDATE agent_artifacts SET {column}=? WHERE artifact_id=?",
                    (previous, str(artifact_id)),
                )


async def test_original_artifact_body_mac_and_policy_rejection_are_read_only(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            link, sealed = await _sealed(actual, db)
            artifact_id = link.plan.review_artifact.artifact_id
            with closing(readonly_database(actual.scenario.session.path)) as session:
                body, seal = session.execute(
                    "SELECT body,publication_seal FROM agent_artifacts WHERE artifact_id=?",
                    (str(artifact_id),),
                ).fetchone()
            faults = (
                ("body", bytes([body[0] ^ 1]) + body[1:]),
                ("publication_seal", bytes([seal[0] ^ 1]) + seal[1:]),
                ("state", "expired"),
                ("workspace_scope", "f" * 64),
            )
            for column, value in faults:
                with _artifact_damage(actual, artifact_id, column, value):
                    poisoned = _business_state(actual)
                    for operation in ("read", "prepare"):
                        with _database(actual, read_only=operation == "read") as caller:
                            caller.execute("BEGIN" if operation == "read" else "BEGIN IMMEDIATE")
                            await _reject(actual, _ledger(actual, caller), caller, operation)
                            if operation == "read":
                                assert caller.total_changes == 0
                            caller.execute("ROLLBACK")
                        _persisted(actual, sealed)
                        assert _business_state(actual) == poisoned

            # synthetic：原正文保护完成后注入拒绝，不伪称真实凭据或策略故障。
            guard = actual.artifacts._publication
            original = guard.check_body
            marker = KernelError("synthetic_artifact_policy_rejection", "合成原审阅正文拒绝")
            for operation in ("read", "prepare"):
                visited = []

                async def refused(*args, visited=visited, **kwargs):
                    await original(*args, **kwargs)
                    if kwargs.get("purpose") == "action_review":
                        visited.append(True)
                        raise marker

                with _database(actual, read_only=operation == "read") as caller:
                    caller.execute("BEGIN" if operation == "read" else "BEGIN IMMEDIATE")
                    with monkeypatch.context() as patch:
                        patch.setattr(guard, "check_body", refused)
                        error = await _reject(actual, _ledger(actual, caller), caller, operation)
                        assert error is marker
                    if operation == "read":
                        assert caller.total_changes == 0
                    caller.execute("ROLLBACK")
                assert visited
                _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_original_cas_missing_core_object_and_parent_materials_never_repair(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            link, sealed = await _sealed(actual, db)
            core, store = link.plan.core, actual.scenario.transactions
            parent = core.baseline.source.workspace.parent_closure.sha256
            manifest = json.loads(store.blob(parent))
            digests = (
                core.fingerprint,
                core.object_scope.objects[0].material.cas_digest,
                parent,
                manifest["chunks"][0]["sha256"],
            )
            for digest in dict.fromkeys(digests):
                path = store._blobs / digest
                missing = path.with_name(path.name + ".fault-missing")
                body = path.read_bytes()
                path.rename(missing)
                try:
                    for operation in ("read", "prepare"):
                        with _database(actual, read_only=operation == "read") as caller:
                            caller.execute("BEGIN" if operation == "read" else "BEGIN IMMEDIATE")
                            await _reject(actual, _ledger(actual, caller), caller, operation)
                            if operation == "read":
                                assert caller.total_changes == 0
                            caller.execute("ROLLBACK")
                        assert not path.exists() and missing.read_bytes() == body
                        _persisted(actual, sealed)
                finally:
                    missing.rename(path)
                assert store.blob(digest) == body

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_synthetic_resource_replacements_after_original_artifact_await_reject(
    tmp_path, config, monkeypatch
):
    """synthetic：真实验证 await 返回后更换同字节资源，不模拟业务 Git 执行。"""

    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            _link, sealed = await _sealed(actual, db)
            ledger = _ledger(actual, db)
            scenario = actual.scenario
            resources = (
                (ledger, "_core_store"),
                (ledger, "_reader"),
                (actual.artifacts, "_publication"),
                (scenario.session, "_publication"),
                (scenario.session, "_runtime_owner_token"),
                (scenario.router, "_audit"),
                (scenario.router, "_plans"),
                (scenario.router, "_snapshot_ports"),
            )
            original = actual.artifacts.verify_reference
            for target, name in resources:
                for operation in ("read", "prepare"):
                    visited = []
                    db.execute("BEGIN IMMEDIATE")
                    with monkeypatch.context() as patch:

                        async def replaced(
                            *args, visited=visited, name=name, target=target, **kwargs
                        ):
                            await original(*args, **kwargs)
                            await asyncio.sleep(0)
                            if not visited:
                                visited.append(name)
                                patch.setattr(target, name, copy.copy(getattr(target, name)))

                        patch.setattr(actual.artifacts, "verify_reference", replaced)
                        await _reject(
                            actual,
                            ledger,
                            db,
                            operation,
                            codes={
                                "git_prepared_link_changed",
                                "git_action_review_host_invalid",
                                "git_user_observation_host_invalid",
                            },
                        )
                    assert visited == [name]
                    db.execute("ROLLBACK")
                    _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_physical_database_file_replacement_after_await_rejects_same_rows(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            _link, sealed = await _sealed(actual, db)
            ledger = _ledger(actual, db)
            path = actual.scenario.state / "git-delivery" / "git-delivery.db"
            saved = path.with_name("git-delivery-original.db")
            original = actual.artifacts.verify_reference
            for operation in ("read", "prepare"):
                replacement = path.with_name("git-delivery-replacement.db")
                with _database(actual, path=replacement) as other:
                    db.backup(other)
                    assert _rows(other) == sealed
                replacement_bytes = replacement.read_bytes()
                original_inode = path.stat().st_ino
                swapped = []

                async def replaced(
                    *args,
                    replacement=replacement,
                    swapped=swapped,
                    original_inode=original_inode,
                    **kwargs,
                ):
                    await original(*args, **kwargs)
                    await asyncio.sleep(0)
                    path.rename(saved)
                    replacement.rename(path)
                    swapped.append(True)
                    assert path.stat().st_ino != original_inode

                db.execute("BEGIN IMMEDIATE")
                try:
                    with monkeypatch.context() as patch:
                        patch.setattr(actual.artifacts, "verify_reference", replaced)
                        await _reject(
                            actual, ledger, db, operation, codes={"git_prepared_link_host_invalid"}
                        )
                    assert swapped == [True]
                    assert path.read_bytes() == replacement_bytes
                finally:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    if swapped:
                        path.unlink()
                        saved.rename(path)
                assert path.stat().st_ino == original_inode
                _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_physical_database_replaced_before_entry_rejects_stale_open_connection(
    tmp_path, config, monkeypatch
):
    """物理故障：入口前同内容换 inode，原已打开连接不得认证脱离固定路径的 DB。"""

    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            _link, sealed = await _sealed(actual, db)
            path = actual.scenario.state / "git-delivery" / "git-delivery.db"
            saved = path.with_name("git-delivery-original.db")
            violations = []
            for operation in ("read", "prepare"):
                replacement = path.with_name("git-delivery-replacement.db")
                with _database(actual, path=replacement) as other:
                    db.backup(other)
                    assert _rows(other) == sealed
                inode = path.stat().st_ino
                db.execute("BEGIN IMMEDIATE")
                path.rename(saved)
                replacement.rename(path)
                try:
                    assert path.stat().st_ino != inode
                    before, changes = _rows(db), db.total_changes
                    ledger = _ledger(actual, db)
                    try:
                        if operation == "read":
                            await ledger.read_all(cancel=CancelToken(), checkpoint=lambda: None)
                        else:
                            await _prepare(actual, ledger)
                    except KernelError:
                        pass
                    except sqlite3.Error as error:
                        # 原固定连接失效也必须走有限业务错误，不能泄露原生 SQLite 异常。
                        violations.append((operation, type(error).__name__, error.sqlite_errorname))
                    else:
                        violations.append((operation, "accepted_stale_connection"))
                    assert db.in_transaction
                    assert _rows(db) == before and db.total_changes == changes
                finally:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    path.unlink()
                    saved.rename(path)
                assert path.stat().st_ino == inode
                _persisted(actual, sealed)
            assert not violations, f"入口前物理 DB 替换未被业务边界拒绝: {violations}"

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_bad_tail_anchor_and_authentication_seals_never_modify_or_reseal(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            _link, sealed = await _sealed(actual, db)
            ledger = _ledger(actual, db)
            faults = (
                "UPDATE git_prefix_anchor SET revision=revision+1",
                "UPDATE git_prefix_anchor SET body_sha256='" + "0" * 64 + "'",
                "UPDATE git_prefix_anchor SET seal=x'00'",
                "UPDATE git_record_publications SET seal=x'00'",
                "UPDATE git_record_publications SET prefix_sha256='" + "f" * 64 + "'",
                "DELETE FROM git_record_publications",
                "DELETE FROM git_prefix_anchor",
            )
            publication = actual.scenario.session._publication
            for sql in faults:
                db.execute("BEGIN IMMEDIATE")
                db.execute(sql)
                poisoned = _rows(db)
                issued = []
                with monkeypatch.context() as patch:
                    for authority in (publication.git, publication.git_prefix):
                        original = authority.issue

                        async def counted(*args, original=original, issued=issued, **kwargs):
                            issued.append(True)
                            return await original(*args, **kwargs)

                        patch.setattr(authority, "issue", counted)
                    await _reject(actual, ledger, db, "read")
                    await _reject(actual, ledger, db, "prepare")
                assert not issued and _rows(db) == poisoned
                db.execute("ROLLBACK")
                _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_synthetic_resource_loss_after_original_seal_await_requires_caller_rollback(
    tmp_path, config, monkeypatch
):
    """synthetic：真实原 Seal await 后撤换宿主，已插入的 partial link 由 caller 回滚。"""

    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            await _genesis(actual, db)
            genesis = _rows(db)
            ledger = _ledger(actual, db)
            authority = actual.scenario.session._publication.git
            original = authority.issue
            resources = (
                (actual.artifacts, "_publication"),
                (actual.scenario.session, "_runtime_owner_token"),
                (ledger, "_core_store"),
            )
            for target, name in resources:
                db.execute("BEGIN IMMEDIATE")
                changes = db.total_changes
                issued = []
                with monkeypatch.context() as patch:

                    async def replaced(*args, issued=issued, target=target, name=name, **kwargs):
                        seal = await original(*args, **kwargs)
                        await asyncio.sleep(0)
                        for table in ("git_product_links", "git_product_link_events"):
                            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
                        issued.append(True)
                        patch.setattr(target, name, copy.copy(getattr(target, name)))
                        return seal

                    patch.setattr(authority, "issue", replaced)
                    with pytest.raises(KernelError):
                        await _prepare(actual, ledger)
                assert issued == [True] and db.in_transaction
                assert db.total_changes > changes
                assert db.execute("SELECT COUNT(*) FROM git_product_links").fetchone() == (1,)
                # 未提交 partial link 对独立连接不可见；产品不能替 caller 自动回滚。
                _persisted(actual, genesis)
                db.execute("ROLLBACK")
                assert _rows(db) == genesis
                _persisted(actual, genesis)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_missing_caller_transaction_never_starts_one_or_publishes(
    tmp_path, config, monkeypatch
):
    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            await _genesis(actual, db)
            genesis, changes = _rows(db), db.total_changes
            ledger = _ledger(actual, db)
            for operation in ("read", "prepare"):
                with pytest.raises(KernelError) as caught:
                    if operation == "read":
                        await ledger.read_all(cancel=CancelToken(), checkpoint=lambda: None)
                    else:
                        await _prepare(actual, ledger)
                assert caught.value.code == "git_delivery_store_transaction_required"
                assert not db.in_transaction
                assert _rows(db) == genesis and db.total_changes == changes
                _persisted(actual, genesis)
            with _database(actual, read_only=True) as ro:
                with pytest.raises(KernelError) as caught:
                    await _ledger(actual, ro).read_all(
                        cancel=CancelToken(), checkpoint=lambda: None
                    )
                assert caught.value.code == "git_delivery_store_transaction_required"
                assert not ro.in_transaction and ro.total_changes == 0
                assert _rows(ro) == genesis

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_real_new_pending_request_does_not_reseal_synthetic_wrong_history(
    tmp_path, config, monkeypatch
):
    """synthetic 原 Key 错误历史；新请求仍为原真实、尚未入账的 SDK pending Route。"""

    async def inspect(actual):
        with _business_unchanged(actual), _database(actual) as db:
            await _genesis(actual, db)
            genesis = _rows(db)
            ledger = _ledger(actual, db)
            db.execute("BEGIN IMMEDIATE")
            link = await _prepare(actual, ledger)
            db.execute("ROLLBACK")
            _persisted(actual, genesis)
            body = encode_product_git_prepared_link(link, checkpoint=lambda: None)
            for fault in ("foreign_route", "foreign_route_with_wrong_approval"):
                columns = list(prepared_link_columns(link, body))
                payload = json.loads(body)
                # 旧错误身份仅用于故障注入，不登记第二个真实 Git Route 或批准。
                columns[0] = str(uuid4())
                if fault == "foreign_route_with_wrong_approval":
                    payload["approval"]["request_fingerprint"] = "0" * 64
                columns[-1] = json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
                db.execute("BEGIN IMMEDIATE")
                await _publish_raw(actual, db, tuple(columns))
                db.execute("COMMIT")
                poisoned = _rows(db)
                assert (
                    db.execute(
                        "SELECT 1 FROM git_product_links WHERE route_id=?",
                        (str(actual.route.plan.execution.plan_id),),
                    ).fetchone()
                    is None
                )
                publication = actual.scenario.session._publication
                issued = []
                db.execute("BEGIN IMMEDIATE")
                with monkeypatch.context() as patch:
                    for authority in (publication.git, publication.git_prefix):
                        original = authority.issue

                        async def counted(*args, original=original, issued=issued, **kwargs):
                            issued.append(True)
                            return await original(*args, **kwargs)

                        patch.setattr(authority, "issue", counted)
                    await _reject(actual, ledger, db, "read")
                    await _reject(actual, ledger, db, "prepare")
                assert not issued
                db.execute("ROLLBACK")
                _persisted(actual, poisoned)
                # 测试隔离清理只恢复已保存的创世锚；生产接口没有修复或补签入口。
                db.execute("BEGIN IMMEDIATE")
                for table in (
                    "git_record_publications",
                    "git_product_link_events",
                    "git_product_links",
                    "git_prefix_anchor",
                ):
                    db.execute(f"DELETE FROM {table}")
                anchor = dict(genesis)["git_prefix_anchor"][0]
                db.execute("INSERT INTO git_prefix_anchor VALUES (?,?,?,?,?,?,?)", anchor)
                db.execute("COMMIT")
                _persisted(actual, genesis)

    await _case(tmp_path, config, monkeypatch, inspect)
