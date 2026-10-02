"""Task Pack 的单 Run 认证宿主：复用产品 Owner、原 Key 和公开保护 Scope。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Never
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.evals.execution_fs import path_present
from harnessix.product_config.contracts import SecretReference
from harnessix.product_config.session_key import (
    KEY_LOAD_TIMEOUT_SECONDS,
    open_product_session_binding,
)
from harnessix.product_config.session_key_codec import OwnedSessionKey
from harnessix.product_config.state_backup_files import PrivateStateTree
from harnessix.product_config.state_backup_validation import original_key
from harnessix.product_config.state_owner import ProductStateOwner, product_state_owner
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.session.store_publication import SessionPublicationBinding

PROVIDER_SECRET = SecretReference(name="eval.provider", version="1")
SESSION_FILE = "session.sqlite"
HISTORY_READ_TIMEOUT_SECONDS = 120.0
_EXISTING_STATE = (
    SESSION_FILE,
    SESSION_FILE + "-wal",
    SESSION_FILE + "-shm",
    "run-state.json",
    "report.json",
    "session-auth",
)


class _FixtureSecrets:
    """无凭据的低层录制评测不能解析材料；真实 Suite 不使用此空 Scope。"""

    def resolve(self, name: str) -> Never:
        raise KernelError("eval_publication_secret_denied", "离线评测没有可解析的凭据")


def provider_publication_scope(api_key_env: str, api_key: str) -> SecretPublicationScope:
    """冻结凭据 Owner 已解析的唯一材料，不读环境、Keychain 或 Provider 私有字段。"""
    return SecretPublicationScope(
        (PROVIDER_SECRET,),
        EnvironmentSecretProvider(
            (EnvironmentSecretSource(PROVIDER_SECRET.name, PROVIDER_SECRET.version, api_key_env),),
            environment={api_key_env: api_key},
        ),
    )


def require_provider_publication_scope(scope: SecretPublicationScope | None) -> None:
    """真实自定义 Factory 由凭据 Owner 显式提供非空保护快照。"""
    if scope is None or not scope.publication_context()["bindings"]:
        raise KernelError("publication_scope_unavailable", "真实评测缺少同源凭据保护 Scope")


def _existing_material(root: Path) -> OwnedSessionKey:
    material: OwnedSessionKey | None = None
    try:
        with PrivateStateTree(root) as tree:
            material = original_key(tree)
    except (KernelError, OSError):
        if material is not None:
            material.close()
        raise KernelError("publication_key_unavailable", "评测原 Session 密钥不可用") from None
    except BaseException:
        if material is not None:
            material.close()
        raise
    return material


async def _load_existing_material(root: Path) -> OwnedSessionKey:
    task = asyncio.create_task(asyncio.to_thread(_existing_material, root))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 原受托线程不能强杀；结算唯一任务并清零迟到材料，再传播父取消。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is None:
            task.result().close()
        raise


@asynccontextmanager
async def _existing_binding(
    root: Path, scope: SecretPublicationScope
) -> AsyncIterator[SessionPublicationBinding]:
    try:
        async with asyncio.timeout(KEY_LOAD_TIMEOUT_SECONDS):
            material = await _load_existing_material(root)
    except TimeoutError:
        raise KernelError("publication_key_timeout", "评测原 Session 密钥加载超时") from None
    binding: SessionPublicationBinding | None = None
    try:
        binding = SessionPublicationBinding(material.store_id, material.key_id, material.key, scope)
        yield binding
    finally:
        if binding is not None:
            binding.close()
        material.close()


@dataclass(frozen=True, slots=True)
class TaskPackHistoryReadControl:
    """一次只读认证阶段的原取消和绝对期限，不延长任何 Turn 执行预算。"""

    cancel: CancelToken
    deadline: float

    @classmethod
    def begin(cls, cancel: CancelToken) -> TaskPackHistoryReadControl:
        return cls(cancel, monotonic() + HISTORY_READ_TIMEOUT_SECONDS)

    def checkpoint(self, owner: TaskPackPublicationOwner) -> None:
        self.cancel.checkpoint()
        if monotonic() >= self.deadline:
            raise KernelError("publication_history_timeout", "Session历史认证超时")
        owner.require_ready(owner.run_root)


@dataclass(frozen=True, slots=True)
class TaskPackPublicationOwner:
    """仅借用于 Run 上下文，三条执行／恢复路径共用原 Store、Binding 与 Scope。"""

    run_root: Path
    root_owner: ProductStateOwner = field(repr=False)
    binding: SessionPublicationBinding = field(repr=False)
    scope: SecretPublicationScope = field(repr=False)
    sessions: SQLiteSessionStore = field(repr=False)
    artifacts: SQLiteArtifactStore = field(repr=False)
    private_tree: PrivateStateTree = field(repr=False)

    def require_ready(self, run_root: Path) -> None:
        """借用前复核原 Run 地址、根外锁及原 Root inode／权限，不只检查锁存在。"""
        if run_root != self.run_root:
            raise KernelError("product_state_owner_invalid", "评测 Run Owner 归属不匹配")
        self.root_owner.require_ready(run_root)
        self.private_tree.checkpoint()

    async def authenticated_thread_ids(self, control: TaskPackHistoryReadControl) -> list[UUID]:
        """身份发现也沿原取消与阶段剩余时间结算，不能在扫描期间刷新 TTL。"""
        control.checkpoint(self)
        try:
            async with asyncio.timeout(max(0.0, control.deadline - monotonic())):
                thread_ids = await control.cancel.run(self.sessions.thread_ids())
        except TimeoutError:
            raise KernelError("publication_history_timeout", "Session历史认证超时") from None
        control.checkpoint(self)
        return thread_ids

    async def authenticated_thread_history(
        self, thread_id: UUID, control: TaskPackHistoryReadControl
    ) -> AuthenticatedThreadHistory:
        """原 Owner 贯穿同读验真；只消费正式 carrier，不另拼投影与事件。"""
        control.checkpoint(self)
        history = await self.sessions.authenticated_thread_history(
            thread_id,
            cancel=control.cancel,
            deadline=control.deadline,
            checkpoint=lambda: self.require_ready(self.run_root),
        )
        control.checkpoint(self)
        return history

    async def authenticated_single_thread(
        self, control: TaskPackHistoryReadControl
    ) -> Thread | None:
        """识别 Run 唯一 Thread，并在交付恢复逻辑前完整认证其同读历史。"""
        control.checkpoint(self)
        thread_ids = await self.authenticated_thread_ids(control)
        if len(thread_ids) > 1:
            raise KernelError("eval_run_projection_invalid", "Task Pack Session包含多个Thread")
        control.checkpoint(self)
        if not thread_ids:
            return None
        return (await self.authenticated_thread_history(thread_ids[0], control)).thread


def _retain_session_file(files: ExitStack, tree: PrivateStateTree) -> None:
    """仅转换取得原叶句柄的系统错误，不把下游业务 OSError 误报为认证失败。"""
    try:
        files.enter_context(tree.open_file(SESSION_FILE, metadata_only=os.name == "nt"))
    except OSError:
        raise KernelError("eval_publication_session_invalid", "评测原 Session 文件无效") from None


@asynccontextmanager
async def open_task_pack_publication(
    run_root: Path,
    publication_scope: SecretPublicationScope | None = None,
    *,
    existing_only: bool = False,
    root_owner: ProductStateOwner | None = None,
    history_read: TaskPackHistoryReadControl | None = None,
) -> AsyncIterator[TaskPackPublicationOwner]:
    """恢复只读原 Key；缺材料、旧 MAC 缺失或验证失败均不得退回初始化。"""
    with ExitStack() as resources:
        scope = publication_scope
        if scope is None:
            scope = resources.enter_context(SecretPublicationScope((), _FixtureSecrets()))
        if root_owner is None:
            root_owner = resources.enter_context(product_state_owner(run_root))
        root_owner.require_ready(run_root)
        private_tree = resources.enter_context(PrivateStateTree(run_root))
        if existing_only and not path_present(run_root / SESSION_FILE):
            raise KernelError("eval_publication_session_missing", "评测原 Session 不存在")
        # 认证目录本身也表示已开始初始化；不能因 key.v1 丢失而生成替代身份。
        existing = existing_only or any(path_present(run_root / name) for name in _EXISTING_STATE)
        binding_context = (
            _existing_binding(run_root, scope)
            if existing
            else open_product_session_binding(run_root, scope)
        )
        async with binding_context as binding:
            private_tree.checkpoint()
            with ExitStack() as files:
                present = path_present(run_root / SESSION_FILE)
                if present:
                    # 固定原叶 FD／Handle，拒绝链接及文件替换；退出时再次验证原对象。
                    _retain_session_file(files, private_tree)
                sessions = SQLiteSessionStore(run_root / SESSION_FILE, publication=binding)
                artifacts = SQLiteArtifactStore(sessions, public_output_protection=scope)
                owner = TaskPackPublicationOwner(
                    run_root, root_owner, binding, scope, sessions, artifacts, private_tree
                )
                if present:
                    # 恢复先走 enroll=False 的正式 Reader；空旧库也不能补签 Header。
                    if history_read is None:
                        await sessions.thread_ids()
                    else:
                        await owner.authenticated_thread_ids(history_read)
                elif any(path_present(run_root / name) for name in _EXISTING_STATE[1:5]):
                    raise KernelError("eval_publication_session_missing", "评测原 Session 不完整")
                private_tree.checkpoint()
                if not present:
                    # 已有库只读认证，不为完成恢复执行迁移、写 Header 或切换 WAL。
                    await sessions.initialize()
                    _retain_session_file(files, private_tree)
                private_tree.checkpoint()
                owner.require_ready(run_root)
                yield owner
                owner.require_ready(run_root)
