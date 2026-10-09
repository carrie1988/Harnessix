"""用户 Git 观察借用同一原 Session/Router/CAS/Scope，不接纳认证替身。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PosixPath, WindowsPath
from types import MethodType

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.publication_seal import EventPublicationAuthority
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.tools.git import GitReadRuntime, _git_arguments
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


def _invalid() -> KernelError:
    """宿主拒绝只公开有限分类，不读取资源错误正文。"""
    return KernelError("git_user_observation_host_invalid", "Git用户观察缺少原有效宿主")


def require_git_user_authority(
    session: SQLiteSessionStore,
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    ports: WorkspaceSnapshotPorts,
    reader: GitReadRuntime,
) -> Callable[[], None]:
    """冻结实际资源对象引用与原固定地址；返回同次操作可复核的宿主检查点。"""
    if (
        type(session) is not SQLiteSessionStore
        or type(router) is not TrustedActionRouter
        or type(transactions) is not SQLiteWorkspaceTransactionStore
        or type(ports) is not WorkspaceSnapshotPorts
        or type(reader) is not GitReadRuntime
        or type(session._publication) is not SessionPublicationBinding
    ):
        raise _invalid()
    publication = session._publication
    events = publication._events
    if type(events) is not EventPublicationAuthority:
        raise _invalid()
    protection = events._protection
    store_id, key_id = publication._store_id, publication._key_id
    if (
        type(events) is not EventPublicationAuthority
        or type(protection) is not SecretPublicationScope
    ):
        raise _invalid()
    audit, plans, state = router._audit, router._plans, session.path.parent
    controlled_read = ports.controlled_read_blob
    root, executable, binding = reader._root, reader._executable, reader.contract()
    process_state = reader._state_directory
    arguments = _git_arguments(for_delivery=True)
    native_state = type(state) in (PosixPath, WindowsPath)
    expected_paths: dict[str, Path] = {}

    def expected_path(name: str) -> Path:
        # 只复用原生Path的固定右值；首次构造仍在原短路条件位置。
        if not native_state:
            return state / name
        if name not in expected_paths:
            expected_paths[name] = state / name
        return expected_paths[name]

    def verify() -> None:
        """不替换、重开或关闭原资源；同字节 Scope 或 Publication 也不能替身。"""
        if (
            session._publication is not publication
            or publication._closed
            or publication._events is not events
            or events._closed
            or events._protection is not protection
            or publication._store_id != store_id
            or publication._key_id != key_id
            or events._key_id != key_id
            or protection._closed
            or reader._output_redaction is not protection
            or router._audit is not audit
            or router._plans is not plans
            or router._snapshot_ports is not ports
            or ports.controlled_read_blob is not controlled_read
            or (
                controlled_read is not None
                and (
                    type(controlled_read) is not MethodType
                    or getattr(controlled_read, "__self__", None) is not transactions
                    or getattr(controlled_read, "__func__", None)
                    is not transactions.controlled_blob.__func__
                )
            )
            or audit._closed
            or plans._closed
            or transactions._closed
            or session.path != expected_path("sessions.db")
            or transactions._root != expected_path("workspace-transactions")
            or audit._path != expected_path("action-audit.db")
            or plans._path != expected_path("execution-plans.db")
            or reader._root != root
            or reader._executable != executable
            or reader._state_directory != process_state
            or reader._global_arguments != arguments
            or reader.contract() != binding
            or getattr(ports.write_blob, "__self__", None) is not transactions
            or getattr(ports.write_blob, "__func__", None) is not transactions.put_blob.__func__
            or getattr(ports.read_blob, "__self__", None) is not transactions
            or getattr(ports.read_blob, "__func__", None) is not transactions.blob.__func__
        ):
            raise _invalid()

    verify()
    return verify
