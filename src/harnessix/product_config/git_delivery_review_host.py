"""Git Review借原有效Session/Artifact发布宿主；不接受Scope或Owner替身。"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_user_authority import require_git_user_authority
from harnessix.sqlite_readonly import readonly_database
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.recovery_contracts import ActionRuntimeFence
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


def _audit_file_identity(path: Path) -> tuple[int, int]:
    """固定已有Audit普通文件，拒绝缺失、符号链接及可观察替换；不宣称FD认证。"""
    try:
        value = path.lstat()
    except OSError:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件不可用") from None
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件不可用")
    return value.st_dev, value.st_ino


def _read_fresh_owner(
    audit: SQLiteActionAuditStore,
    path: Path,
    identity: tuple[int, int],
    *,
    original: sqlite3.Connection,
) -> None:
    """短只读视图独立于原遗留游标；不新建Store、复制Owner算法或关闭原连接。"""
    if _audit_file_identity(path) != identity:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件已经变化")
    try:
        observer = readonly_database(path)
        if observer is original or observer is audit._db:
            raise KernelError("git_action_review_host_invalid", "Git审阅只读观察连接无效")
        if type(observer) is not sqlite3.Connection:
            if isinstance(observer, sqlite3.Connection):
                # 拒绝子类且通过原生基类关闭，不能执行可覆盖的close回调。
                sqlite3.Connection.close(observer)
            raise KernelError("git_action_review_host_invalid", "Git审阅只读观察连接无效")
        with closing(observer):
            if observer.in_transaction or _audit_file_identity(path) != identity:
                raise KernelError("git_action_review_host_invalid", "Git审阅只读观察已经变化")
            audit._read_runtime_owner(database=observer)
    except (sqlite3.Error, OSError):
        raise KernelError("git_action_review_host_invalid", "Git审阅原所有权观察不可用") from None
    if _audit_file_identity(path) != identity:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件已经变化")


def require_git_review_host(
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    reader: GitReadRuntime,
    ports: WorkspaceSnapshotPorts,
    workspace_scope: str,
) -> Callable[[], None]:
    """同次操作冻结原发布Guard/Session/Scope/Owner，原用户宿主检查继续生效。"""
    if (
        type(core_store) is not ProductGitDeliveryCoreStore
        or type(artifacts) is not SQLiteArtifactStore
        or type(workspace_scope) is not str
        or len(workspace_scope) != 64
        or any(char not in "0123456789abcdef" for char in workspace_scope)
        or type(artifacts._publication) is not ArtifactPublicationGuard
    ):
        raise KernelError("git_action_review_host_invalid", "Git审阅缺少原有效发布宿主")
    session, transactions, guard = artifacts.session, core_store.store, artifacts._publication
    original = require_git_user_authority(session, router, transactions, ports, reader)
    audit = router._audit
    if type(audit) is not SQLiteActionAuditStore:
        raise KernelError("git_action_review_host_invalid", "Git审阅缺少原有效Audit宿主")
    fence, database = audit._runtime_fence, audit._db
    if type(fence) is not ActionRuntimeFence or type(database) is not sqlite3.Connection:
        raise KernelError("git_action_review_host_invalid", "Git审阅缺少原活跃Audit所有权")
    fence_fields = (fence.generation, fence.token, fence.acquired_at)
    path = audit._path
    identity = _audit_file_identity(path)
    publication, owner = session._publication, session._runtime_owner_token
    assert publication is not None
    protection = publication._events._protection

    def bound() -> None:
        """同字节对象不能替换原活跃身份；不重开认证资源或补签发布证明。"""
        original()
        if (
            owner is None
            or session._runtime_owner_token is not owner
            or artifacts.session is not session
            or core_store.store is not transactions
            or artifacts._publication is not guard
            or guard.binding is not publication
            or guard.protection is not protection
            or reader.contract()["implementation"] != "git-baseline-read/v1"
            or audit._db is not database
            or database.in_transaction
            or audit._require_runtime_owner is not True
            or audit._runtime_fence is not fence
            or (fence.generation, fence.token, fence.acquired_at) != fence_fields
        ):
            raise KernelError("git_action_review_host_invalid", "Git审阅原发布宿主已经变化")

    def check() -> None:
        """首末复核原身份；补充短只读视图，不能仅信任自动提交标志。"""
        bound()
        audit._read_runtime_owner()
        _read_fresh_owner(audit, path, identity, original=database)
        bound()

    check()
    return check
