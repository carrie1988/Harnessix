"""Git Review借原有效Session/Artifact发布宿主；不接受Scope或Owner替身。"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.agent.errors import KernelError
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_user_authority import require_git_user_authority
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


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
    publication, owner = session._publication, session._runtime_owner_token
    assert publication is not None
    protection = publication._events._protection

    def check() -> None:
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
        ):
            raise KernelError("git_action_review_host_invalid", "Git审阅原发布宿主已经变化")

    check()
    return check
