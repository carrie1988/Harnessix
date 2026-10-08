"""实际认证 SDK 的末端变化回归；故障注入不代表业务 Git 执行成功。"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import utc_now
from harnessix.product_config import git_prepared_link_ledger as module
from harnessix.product_config.git_prefix_sql import (
    git_prefix_sql_window,
    git_prefix_transaction_epoch,
    require_git_prefix_transaction_epoch,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.product_config.test_git_prepared_link_controls import (
    _business_unchanged,
    _persisted,
    _sealed,
)
from tests.product_config.test_git_prepared_link_ledger import _case, _database, _ledger, _rows


def test_terminal_epoch_check_does_not_reenter_callback_and_rejects_new_transaction():
    """原 SQL 代际的无回调复核负控；不涉及 Session 或业务认证。"""
    seen = []
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as db:
        db.execute("BEGIN")
        with git_prefix_sql_window(db, checkpoint=lambda: seen.append(True)):
            epoch = git_prefix_transaction_epoch(db)
            calls = len(seen)
            require_git_prefix_transaction_epoch(db, epoch)
            assert len(seen) == calls
            db.execute("ROLLBACK")
            db.execute("BEGIN")
            with pytest.raises(KernelError) as caught:
                require_git_prefix_transaction_epoch(db, epoch)
            assert caught.value.code == "publication_history_unproven"
            assert len(seen) == calls
        with pytest.raises(KernelError):
            require_git_prefix_transaction_epoch(db, epoch)


async def test_original_session_commit_after_actual_authentication_is_rejected(
    tmp_path, config, monkeypatch
):
    """原 Session 跨连接写后还原也拒绝；零物理变更的空提交不视为状态变化。"""

    async def inspect(actual):
        with _business_unchanged(actual):
            async with _database(actual) as db:
                link, sealed = await _sealed(actual, db)
                authenticate = module._authenticate

                async def changed(*args, **kwargs):
                    evidence = await authenticate(*args, **kwargs)
                    with closing(sqlite3.connect(actual.scenario.session.path)) as write:
                        identity = str(link.plan.review_artifact.artifact_id)
                        body = write.execute(
                            "SELECT body FROM agent_artifacts WHERE artifact_id=?", (identity,)
                        ).fetchone()[0]
                        cursor = write.execute(
                            "UPDATE agent_artifacts SET body=? WHERE artifact_id=?",
                            (b"X" + body[1:], identity),
                        )
                        assert cursor.rowcount == 1
                        write.commit()
                        write.execute(
                            "UPDATE agent_artifacts SET body=? WHERE artifact_id=?",
                            (body, identity),
                        )
                        write.commit()
                    return evidence

                for operation in ("read", "prepare"):
                    db.execute("BEGIN IMMEDIATE")
                    with monkeypatch.context() as patch:
                        patch.setattr(module, "_authenticate", changed)
                        ledger = _ledger(actual, db)
                        with pytest.raises(KernelError) as caught:
                            if operation == "read":
                                await ledger.read_all(cancel=CancelToken(), checkpoint=lambda: None)
                            else:
                                await ledger.prepare(
                                    actual.route.plan.execution.plan_id,
                                    cancel=CancelToken(),
                                    checkpoint=lambda: None,
                                )
                    assert caught.value.code == "git_prepared_link_changed"
                    assert db.in_transaction and _rows(db) == sealed
                    db.execute("ROLLBACK")
                    await _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_final_history_await_cas_and_review_changes_are_rejected(
    tmp_path, config, monkeypatch
):
    """原完整认证及 U 验证返回后移除 Git Commit 材料或破坏 Review，末端必须拒绝。"""

    async def inspect(actual):
        with _business_unchanged(actual):
            async with _database(actual) as db:
                link, sealed = await _sealed(actual, db)
                session = actual.scenario.session
                material = next(
                    node.material
                    for node in link.plan.core.object_scope.objects
                    if node.material.object_type == "commit"
                )
                path = actual.scenario.transactions._blobs / material.cas_digest
                moved = path.with_name(path.name + ".terminal-missing")
                ref = link.plan.review_artifact
                with closing(sqlite3.connect(session.path)) as read:
                    body = read.execute(
                        "SELECT body FROM agent_artifacts WHERE artifact_id=?",
                        (str(ref.artifact_id),),
                    ).fetchone()[0]
                authenticate = module._authenticate
                for fault in ("cas", "review"):
                    for operation in ("read", "prepare"):
                        visited = []
                        last_authentication = 1 if operation == "read" else 2

                        async def after_last_authentication(
                            *args,
                            fault=fault,
                            visited=visited,
                            last_authentication=last_authentication,
                            **kwargs,
                        ):
                            evidence = await authenticate(*args, **kwargs)
                            visited.append(True)
                            if len(visited) == last_authentication:
                                # 单条关联回读一次，精确重试再认证目标；完整 U 已验证成功。
                                if fault == "cas":
                                    path.rename(moved)
                                else:
                                    with closing(sqlite3.connect(session.path)) as write:
                                        write.execute(
                                            "UPDATE agent_artifacts SET body=? WHERE artifact_id=?",
                                            (b"X" + body[1:], str(ref.artifact_id)),
                                        )
                                        write.commit()
                            return evidence

                        db.execute("BEGIN IMMEDIATE")
                        changes = db.total_changes
                        try:
                            with monkeypatch.context() as patch:
                                patch.setattr(module, "_authenticate", after_last_authentication)
                                ledger = _ledger(actual, db)
                                with pytest.raises(KernelError):
                                    if operation == "read":
                                        await ledger.read_all(
                                            cancel=CancelToken(), checkpoint=lambda: None
                                        )
                                    else:
                                        await ledger.prepare(
                                            actual.route.plan.execution.plan_id,
                                            cancel=CancelToken(),
                                            checkpoint=lambda: None,
                                        )
                            assert len(visited) == last_authentication
                            assert db.in_transaction and _rows(db) == sealed
                            assert db.total_changes == changes
                        finally:
                            db.execute("ROLLBACK")
                            if fault == "cas":
                                assert moved.is_file() and not path.exists()
                                moved.rename(path)
                            else:
                                with closing(sqlite3.connect(session.path)) as write:
                                    write.execute(
                                        "UPDATE agent_artifacts SET body=? WHERE artifact_id=?",
                                        (body, str(ref.artifact_id)),
                                    )
                                    write.commit()
                        await _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_anchor_only_write_and_restore_after_authentication_is_rejected(
    tmp_path, config, monkeypatch
):
    """仅同连接尾锚写后还原也必须拒绝，不能只比较十二个业务表的最终字节。"""

    async def inspect(actual):
        with _business_unchanged(actual):
            async with _database(actual) as db:
                _link, sealed = await _sealed(actual, db)
                original = module._authenticate

                async def changed(*args, **kwargs):
                    evidence = await original(*args, **kwargs)
                    db.execute("UPDATE git_prefix_anchor SET revision=revision+1")
                    db.execute("UPDATE git_prefix_anchor SET revision=revision-1")
                    return evidence

                for operation in ("read", "prepare"):
                    db.execute("BEGIN IMMEDIATE")
                    changes = db.total_changes
                    with monkeypatch.context() as patch:
                        patch.setattr(module, "_authenticate", changed)
                        ledger = _ledger(actual, db)
                        with pytest.raises(KernelError) as caught:
                            if operation == "read":
                                await ledger.read_all(cancel=CancelToken(), checkpoint=lambda: None)
                            else:
                                await ledger.prepare(
                                    actual.route.plan.execution.plan_id,
                                    cancel=CancelToken(),
                                    checkpoint=lambda: None,
                                )
                    assert caught.value.code == "git_prepared_link_changed"
                    assert db.in_transaction and _rows(db) == sealed
                    assert db.total_changes == changes + 2
                    db.execute("ROLLBACK")
                    await _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


async def test_last_external_sql_callback_cancel_or_expiry_cannot_return_success(
    tmp_path, config, monkeypatch
):
    """在原 SQL 窗口正常退出的最后一次外部回调注入，不提前取消以掩盖空窗。"""

    async def inspect(actual):
        with _business_unchanged(actual):
            async with _database(actual) as db:
                _link, sealed = await _sealed(actual, db)
                original_window, authenticate = module.git_prefix_sql_window, module._authenticate
                for fault in ("cancel", "deadline"):
                    token, controls = CancelToken(), {}

                    async def capture(*args, controls=controls, **kwargs):
                        controls["budget"] = args[3]
                        return await authenticate(*args, **kwargs)

                    @contextmanager
                    def final_callback(database, *, checkpoint, controls=controls):
                        with original_window(database, checkpoint=checkpoint):
                            yield
                            controls["last_external_callback"] = True

                    def checkpoint(controls=controls, fault=fault, token=token):
                        if controls.pop("last_external_callback", False):
                            controls["injected"] = True
                            if fault == "cancel":
                                token.cancel()
                            else:
                                controls["budget"]._deadline = 0

                    db.execute("BEGIN IMMEDIATE")
                    with monkeypatch.context() as patch:
                        patch.setattr(module, "git_prefix_sql_window", final_callback)
                        patch.setattr(module, "_authenticate", capture)
                        with pytest.raises(
                            TurnCancelled if fault == "cancel" else KernelError
                        ) as caught:
                            await _ledger(actual, db).read_all(cancel=token, checkpoint=checkpoint)
                    if fault == "deadline":
                        assert caught.value.code == "git_process_timeout"
                    assert controls["injected"]
                    assert db.in_transaction and _rows(db) == sealed
                    db.execute("ROLLBACK")
                    await _persisted(actual, sealed)

    await _case(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("operation", ["read", "verify"])
async def test_read_only_artifact_missing_session_never_creates_database(tmp_path, operation):
    """缺失文件 API 负控：不建立原认证宿主，不把普通声明当作阳性凭据。"""
    path = tmp_path / "missing-session.db"
    store = SQLiteArtifactStore(SQLiteSessionStore(path))
    ref = ArtifactRef(
        artifact_id=uuid4(),
        sha256="0" * 64,
        size_bytes=1,
        records=1,
        complete=True,
        expires_at=utc_now() + timedelta(hours=1),
    )
    with pytest.raises(KernelError):
        if operation == "read":
            await store.read(uuid4(), "0" * 64, ref.artifact_id, read_only=True)
        else:
            await store.verify_reference(
                uuid4(),
                uuid4(),
                ref,
                workspace_scope="0" * 64,
                purpose="action_review",
                read_only=True,
            )
    assert not path.exists() and not list(tmp_path.iterdir())
