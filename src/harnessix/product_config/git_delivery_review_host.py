"""Git Review借原有效Session/Artifact发布宿主；不接受Scope或Owner替身。"""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config import git_prepared_native_identity as native
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_user_authority import require_git_user_authority
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.publication_seal import EventPublicationAuthority
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.sqlite_readonly import readonly_database
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.recovery_contracts import ActionRuntimeFence
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts

_READER_METADATA_FIELDS = (
    "_output_redaction",
    "_root",
    "_executable",
    "_state_directory",
    "_global_arguments",
    "_for_delivery",
    "_binding_fingerprint",
    "contract",
)


def _metadata_fields(subject: object, kind: type[object], code: str) -> dict[str, object]:
    """先拒绝可执行字典/键，再重建原生字符串索引；不触发字段属性或比较。"""
    if type(subject) is not kind:
        raise KernelError(code, "Git原资源元数据已经变化")
    current = object.__getattribute__(subject, "__dict__")
    if type(current) is not dict:
        raise KernelError(code, "Git原资源元数据已经变化")
    fields = tuple(current.items())
    if any(type(name) is not str for name, _ in fields):
        raise KernelError(code, "Git原资源元数据已经变化")
    return dict(fields)


def _selected_fields_guard(
    subject: object, kind: type[object], names: tuple[str, ...], code: str
) -> Callable[[], None]:
    """仅冻结必要字段及方法shadow的原引用；未绑定的合法缓存可继续更新。"""
    missing = object()
    initial = _metadata_fields(subject, kind, code)
    selected = tuple((name, initial.get(name, missing)) for name in names)

    def check() -> None:
        current = _metadata_fields(subject, kind, code)
        if any(current.get(name, missing) is not value for name, value in selected):
            raise KernelError(code, "Git原资源元数据已经变化")

    return check


def _git_review_metadata(
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    reader: GitReadRuntime,
    ports: WorkspaceSnapshotPorts,
) -> Callable[[], None]:
    """只借原全认证后的元数据；不调用旧bound、动态方法、Path比较或Owner查询。"""
    user_code, review_code = "git_user_observation_host_invalid", "git_action_review_host_invalid"
    artifact_fields = _metadata_fields(artifacts, SQLiteArtifactStore, review_code)
    session = artifact_fields.get("_session")
    publication = _metadata_fields(session, SQLiteSessionStore, user_code).get("_publication")
    events = _metadata_fields(publication, SessionPublicationBinding, user_code).get("_events")
    protection = _metadata_fields(events, EventPublicationAuthority, user_code).get("_protection")
    router_fields = _metadata_fields(router, TrustedActionRouter, user_code)
    audit, plans = router_fields.get("_audit"), router_fields.get("_plans")
    audit_fields = _metadata_fields(audit, SQLiteActionAuditStore, user_code)
    database, fence = audit_fields.get("_db"), audit_fields.get("_runtime_fence")
    if type(core_store) is not ProductGitDeliveryCoreStore:
        raise KernelError(review_code, "Git审阅原资源元数据已经变化")
    if type(ports) is not WorkspaceSnapshotPorts:
        raise KernelError(user_code, "Git原资源元数据已经变化")
    transactions = object.__getattribute__(core_store, "store")
    write_blob = object.__getattribute__(ports, "write_blob")
    read_blob = object.__getattribute__(ports, "read_blob")
    user_checks = tuple(
        _selected_fields_guard(subject, kind, names, user_code)
        for subject, kind, names in (
            (session, SQLiteSessionStore, ("_publication", "path")),
            (
                publication,
                SessionPublicationBinding,
                ("_closed", "_events", "_store_id", "_key_id"),
            ),
            (events, EventPublicationAuthority, ("_closed", "_protection", "_key_id")),
            (protection, SecretPublicationScope, ("_closed",)),
            (router, TrustedActionRouter, ("_audit", "_plans", "_snapshot_ports")),
            (audit, SQLiteActionAuditStore, ("_closed", "_path", "_checkpoint", "_read_blob")),
            (plans, SQLiteExecutionPlanStore, ("_closed", "_path")),
            (
                transactions,
                SQLiteWorkspaceTransactionStore,
                ("_closed", "_root", "_checkpoint", "put_blob", "blob"),
            ),
            (reader, GitReadRuntime, _READER_METADATA_FIELDS),
        )
    )
    review_checks = tuple(
        _selected_fields_guard(subject, kind, names, review_code)
        for subject, kind, names in (
            (session, SQLiteSessionStore, ("_runtime_owner_token",)),
            (artifacts, SQLiteArtifactStore, ("_session", "_publication", "session")),
            (
                artifact_fields.get("_publication"),
                ArtifactPublicationGuard,
                ("binding", "protection"),
            ),
            (
                audit,
                SQLiteActionAuditStore,
                ("_db", "_runtime_fence", "_require_runtime_owner", "_read_runtime_owner"),
            ),
            (fence, ActionRuntimeFence, ("generation", "token", "acquired_at")),
        )
    )

    def observe() -> None:
        for check in user_checks:
            check()
        if (
            type(ports) is not WorkspaceSnapshotPorts
            or object.__getattribute__(ports, "write_blob") is not write_blob
            or object.__getattribute__(ports, "read_blob") is not read_blob
        ):
            raise KernelError(user_code, "Git原CAS端口元数据已经变化")
        for check in review_checks:
            check()
        if (
            type(core_store) is not ProductGitDeliveryCoreStore
            or object.__getattribute__(core_store, "store") is not transactions
            or type(database) is not sqlite3.Connection
        ):
            raise KernelError(review_code, "Git审阅原资源元数据已经变化")
        try:
            if database.in_transaction is not False:
                raise KernelError(review_code, "Git审阅原连接元数据已经变化")
        except sqlite3.Error:
            raise KernelError(review_code, "Git审阅原连接不可用") from None

    return observe


def _audit_file_identity(path: Path) -> tuple[int, int]:
    """固定已有Audit普通文件，拒绝缺失、符号链接及可观察替换；不宣称FD认证。"""
    try:
        value = path.lstat()
    except OSError:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件不可用") from None
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise KernelError("git_action_review_host_invalid", "Git审阅原Audit文件不可用")
    return value.st_dev, value.st_ino


@contextmanager
def _observe_review_connection(
    database: sqlite3.Connection, identity: tuple[int, int]
) -> Iterator[Callable[[], None]]:
    """核验新鲜读连接的既有 pin；短期令牌不能用于原 Audit 的重复绑定。"""
    try:
        backend = native.prepared_identity_backend()
        token = native.attach_prepared_identity(backend, database, identity)
    except KernelError as error:
        if error.code == "git_prepared_link_host_invalid":
            raise KernelError(
                "git_action_review_host_invalid", "Git审阅原生连接身份不可用"
            ) from None
        raise
    active = True

    def check() -> None:
        if not active:
            raise KernelError("git_action_review_host_invalid", "Git审阅连接观察已经结束")
        try:
            native.check_prepared_identity(token)
        except KernelError as error:
            if error.code == "git_prepared_link_host_invalid":
                raise KernelError(
                    "git_action_review_host_invalid", "Git审阅原生连接身份不可用"
                ) from None
            raise

    failed = False
    try:
        check()
        yield check
    except BaseException:
        failed = True
        raise
    finally:
        active = False
        # 先撤销闭包，再释放令牌；清理错误不能覆盖 Owner、取消或超时的首失败。
        try:
            if token is not None:
                token.release()
        except BaseException as error:
            if not failed:
                if backend is not None and isinstance(error, (backend.BridgeError, sqlite3.Error)):
                    raise KernelError(
                        "git_action_review_host_invalid", "Git审阅原生连接身份不可用"
                    ) from None
                raise


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
        with closing(observer), _observe_review_connection(observer, identity) as fresh_identity:
            if observer.in_transaction or _audit_file_identity(path) != identity:
                raise KernelError("git_action_review_host_invalid", "Git审阅只读观察已经变化")
            audit._read_runtime_owner(database=observer)
            fresh_identity()
    except TimeoutError:
        # TimeoutError 也是 OSError；上游期限不能被下方存储错误分类吞掉。
        raise
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
    return _git_review_host_checks(router, core_store, artifacts, reader, ports, workspace_scope)[1]


def _git_review_host_checks(
    router: TrustedActionRouter,
    core_store: ProductGitDeliveryCoreStore,
    artifacts: SQLiteArtifactStore,
    reader: GitReadRuntime,
    ports: WorkspaceSnapshotPorts,
    workspace_scope: str,
) -> tuple[Callable[[], None], Callable[[], None]]:
    """复用原冻结流程与初始全认证；第一项仅观察元数据，不授予Owner授权。"""
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

    def full() -> None:
        """首末复核原身份；补充短只读视图，不能仅信任自动提交标志。"""
        bound()
        audit._read_runtime_owner()
        _read_fresh_owner(audit, path, identity, original=database)
        bound()

    full()
    return _git_review_metadata(router, core_store, artifacts, reader, ports), full
