"""待审批全集的原状态变化检测与终端读集合；不是跨库事务或历史认证替代。"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_prefix_rows import GitPrefixRows, capture_git_prefix_rows
from harnessix.product_config.git_prepared_link_proof import (
    PreparedLinkEvidence,
    prepared_link_changed,
    verify_prepared_link_terminal,
)
from harnessix.product_config.git_user_source_scope import GitUserSourceScope
from harnessix.sqlite_readonly import readonly_database
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from harnessix.workspace.terminal_read_control import terminal_read_scope


def _identity(path: Path) -> tuple[int, int]:
    try:
        value = path.lstat()
    except OSError:
        raise prepared_link_changed() from None
    if not stat.S_ISREG(value.st_mode):
        raise prepared_link_changed()
    return value.st_dev, value.st_ino


def _reader(path: Path) -> sqlite3.Connection:
    """监视连接失败只发布固定错误，不暴露 SQLite 文件或路径信息。"""
    try:
        return readonly_database(path)
    except sqlite3.Error:
        raise prepared_link_changed() from None


def _version(database: sqlite3.Connection) -> int:
    try:
        with closing(database.execute("PRAGMA data_version")) as cursor:
            row = cursor.fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not int:
            raise prepared_link_changed()
        return int(row[0])
    except sqlite3.Error:
        raise prepared_link_changed() from None


@contextmanager
def observe_prepared_state(
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    *,
    check_on_exit: bool = True,
) -> Iterator[Callable[[], None]]:
    """固定只读监视连接覆盖所有 await；发现库变化、原连接写或替换即拒绝。"""
    paths = (
        artifacts.session.path,
        router._audit._path,
        router._plans._path,
        core_store.store._path,
    )
    writers = (router._audit._db, router._plans._db, core_store.store._db)
    with ExitStack() as stack:
        identities = tuple(_identity(path) for path in paths)
        readers = tuple(stack.enter_context(closing(_reader(path))) for path in paths)
        versions = tuple(_version(db) for db in readers)
        changes = tuple(db.total_changes for db in writers)

        def unchanged() -> None:
            current = (router._audit._db, router._plans._db, core_store.store._db)
            if any(a is not b for a, b in zip(current, writers, strict=True)):
                raise prepared_link_changed()
            if tuple(_identity(path) for path in paths) != identities or (
                tuple(_version(db) for db in readers) != versions
                or tuple(db.total_changes for db in writers) != changes
            ):
                raise prepared_link_changed()

        unchanged()
        yield unchanged
        # 提交资源的清理只关闭 reader；提交前已完整复核，不在提交后发布迟到拒绝。
        if check_on_exit:
            unchanged()


@dataclass(slots=True)
class PreparedLinkReadSet:
    """私有当前操作读集合；保存已经验证的事实与精确全行/尾锚，不持有签发能力。"""

    evidence: dict[UUID, PreparedLinkEvidence] = field(default_factory=dict, repr=False)
    rows: GitPrefixRows | None = field(default=None, repr=False)
    anchor: tuple[object, ...] | None = field(default=None, repr=False)
    changes: int | None = None
    source_scope: GitUserSourceScope = field(default_factory=GitUserSourceScope, repr=False)

    def complete(
        self, rows: GitPrefixRows, anchor: tuple[object, ...] | None, changes: int
    ) -> None:
        """仅在完整业务回读结束时固定同次读边界；发布后的新回读可以替换边界。"""
        self.rows = rows
        self.anchor = anchor
        self.changes = changes

    def require_sql(self, database: sqlite3.Connection, check: Callable[[], None]) -> None:
        """全表及独立尾锚都必须保持，含修改后还原的同连接写入。"""
        if (
            self.rows is None
            or self.changes != database.total_changes
            or (
                database.execute("SELECT * FROM git_prefix_anchor").fetchone() != self.anchor
                or capture_git_prefix_rows(database, checkpoint=check) != self.rows
            )
        ):
            raise prepared_link_changed()

    def terminal(
        self,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        artifacts: SQLiteArtifactStore,
        ports: WorkspaceSnapshotPorts,
        workspace_scope: str,
        check: Callable[[], None],
    ) -> None:
        """全集同步重验原事实；底层原 Store/Audit 也不再进入共享构造回调。"""
        with terminal_read_scope(
            core_store.store, router._audit, core_store.store._read_blob, check
        ):
            for evidence in self.evidence.values():
                check()
                verify_prepared_link_terminal(
                    evidence,
                    router,
                    core_store,
                    artifacts,
                    ports,
                    workspace_scope,
                    checkpoint=check,
                )
                self.source_scope.require(evidence.link.plan.core.user_observation, check)
            check()
