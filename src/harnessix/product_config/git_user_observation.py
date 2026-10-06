"""原认证 Patch → 完整 Source2 → 用户 Git 物理观察；不把脏 U 伪装为干净 A。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config.git_baseline import (
    _BASELINE_TIMEOUT_SECONDS,
    _collect_baseline_from_source,
    _Observation,
    _observe,
    _Queries,
    _root_binding_matches,
)
from harnessix.product_config.git_delivery_plan_contracts import GitIndexFileObservation
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_delivery_source import (
    _verify_final_snapshot,
    collect_git_delivery_source,
)
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.git_user_authority import require_git_user_authority
from harnessix.product_config.git_user_observation_contracts import (
    ProductGitUserObservation,
    product_git_user_observation_fingerprint,
)
from harnessix.product_config.git_user_observation_paths import (
    PinnedGitUserDirectories,
    git_user_directory_facts,
    invalid_git_user_paths,
    pin_git_user_directories,
    reported_git_path,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


def git_user_observation_implementation_digest() -> str:
    """本观察配方、原来源与基准共同入摘要；实现替换不能沿用旧观察。"""
    root = Path(__file__).parent
    try:
        return canonical_digest(
            {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "git_user_observation.py",
                    "git_user_observation_contracts.py",
                    "git_user_observation_paths.py",
                    "git_user_authority.py",
                    "git_baseline.py",
                    "git_delivery_source.py",
                    "workspace_patch_source.py",
                )
            }
        )
    except OSError:
        raise KernelError("git_user_observation_unavailable", "Git用户观察实现不可证明") from None


async def _reports(query: _Queries, root: Path) -> tuple[Path, Path]:
    """所有路径只来自固定完整查询；Index 必须是原 admin 根的固定成员。"""
    common = reported_git_path(await query.full("rev-parse", "--git-common-dir"), root)
    admin = reported_git_path(
        await query.full("rev-parse", "--absolute-git-dir"), root, absolute=True
    )
    index = reported_git_path(await query.full("rev-parse", "--git-path", "index"), root)
    if index != admin / "index":
        raise invalid_git_user_paths()
    return common, admin


async def _configuration(query: _Queries) -> str:
    """绑定完整 origin/name/value 私有观察，只保存 SHA，不公开配置正文。"""
    result = await query.result("config", "--no-includes", "--null", "--list", "--show-origin")
    result.full_stdout()  # 证明完整且与 raw 同摘要；不据此声称 POSIX 已脱敏。
    return result.raw_stdout.sha256


async def collect_product_git_user_observation(
    thread: Thread,
    targets: tuple[UUID, ...],
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    reader: GitReadRuntime,
    *,
    session: SQLiteSessionStore,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
    snapshot_ports: WorkspaceSnapshotPorts,
) -> ProductGitUserObservation:
    """先认证实际历史，借原 CAS 捕获一次 Source2，再完整前后复核用户仓库。"""
    await asyncio.sleep(0)
    if (
        type(cancel) is not CancelToken
        or type(budget) is not GitOperationBudget
        or not callable(checkpoint)
    ):
        raise KernelError("git_user_observation_host_invalid", "Git用户观察缺少原有效宿主")
    host_check = require_git_user_authority(session, router, transactions, snapshot_ports, reader)
    deadline = min(time.monotonic() + _BASELINE_TIMEOUT_SECONDS, budget._deadline)
    control_error: BaseException | None = None

    def raw_check() -> None:
        nonlocal control_error
        try:
            cancel.checkpoint()
            budget.remaining()
            checkpoint()
            host_check()
            if time.monotonic() >= deadline:
                raise KernelError("git_baseline_timeout", "Git交付基准总期限已耗尽")
        except BaseException as error:
            control_error = error
            raise

    check = parent_cancel_checkpointer(raw_check)
    check()
    if type(thread) is not Thread or type(reader) is not GitReadRuntime:
        raise KernelError("git_user_observation_host_invalid", "Git用户观察缺少原有效宿主")
    try:
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            return await cancel.run(
                _collect(
                    thread,
                    targets,
                    router,
                    transactions,
                    reader,
                    session,
                    cancel,
                    snapshot_ports,
                    check,
                    deadline,
                )
            )
    except TimeoutError as error:
        if error is control_error:
            raise
        raise KernelError("git_baseline_timeout", "Git交付基准总期限已耗尽") from None
    except (ReadToolError, UnicodeError, OSError) as error:
        if error is control_error:
            raise
        raise KernelError(
            "git_user_observation_unavailable", "Git用户观察无法完成完整读取"
        ) from None


def _native_checkpointer(check: Callable[[], None]) -> Callable[[], None]:
    """只在原生来源与快照边界隔离控制异常，不改写认证Reader的取消语义。"""

    def controlled() -> None:
        try:
            check()
        except UpstreamCheckpointError:
            raise
        except BaseException as error:
            raise UpstreamCheckpointError(error) from None

    return controlled


async def _collect(
    thread: Thread,
    targets: tuple[UUID, ...],
    router: TrustedActionRouter,
    transactions: SQLiteWorkspaceTransactionStore,
    reader: GitReadRuntime,
    session: SQLiteSessionStore,
    cancel: CancelToken,
    ports: WorkspaceSnapshotPorts,
    check: Callable[[], None],
    deadline: float,
) -> ProductGitUserObservation:
    """同一历史与物理窗口；实际执行权仍须新规划、原审批及再次原生验证。"""
    history = await session.authenticated_thread_history(
        thread.thread_id,
        cancel=cancel,
        deadline=deadline,
        checkpoint=check,
    )
    check()
    if history.thread != thread:
        raise KernelError("git_user_observation_history_changed", "Git用户观察会话历史已经变化")
    try:
        source = collect_git_delivery_source(
            history.thread,
            targets,
            router,
            transactions,
            checkpoint=_native_checkpointer(check),
            snapshot_ports=ports,
        )
    except UpstreamCheckpointError as error:
        raise error.error from None
    if type(source) is not ProductGitDeliverySourceV2:
        raise KernelError("workspace_closure_unavailable", "Git用户观察需要完整新代际来源")
    return await _observe_user_baseline(
        source,
        history,
        session,
        reader,
        cancel,
        ports,
        check,
        deadline,
    )


async def _observe_user_baseline(
    source: ProductGitDeliverySourceV2,
    history: AuthenticatedThreadHistory,
    session: SQLiteSessionStore,
    reader: GitReadRuntime,
    cancel: CancelToken,
    ports: WorkspaceSnapshotPorts,
    check: Callable[[], None],
    deadline: float,
) -> ProductGitUserObservation:
    """原基准算法被完整物理前后观察包围；借用原句柄直至会话复核完成。"""
    if reader.contract()["implementation"] != "git-baseline-read/v1":
        raise KernelError("git_baseline_reader_required", "Git用户观察需要固定交付读取端口")
    if not _root_binding_matches(source, reader._root, check):
        raise KernelError("git_baseline_workspace_mismatch", "Git用户观察不属于原认证Workspace")
    implementation = git_user_observation_implementation_digest()
    query = _Queries(reader, cancel, check)
    root, binding = reader._root, reader.contract()
    before = await _reports(query, root)
    config = await _configuration(query)
    with pin_git_user_directories(*before, checkpoint=check) as pinned:
        index = pinned.observe_index(check)

        try:
            baseline = await _collect_baseline_from_source(
                source,
                history.thread,
                reader,
                cancel=cancel,
                snapshot_ports=ports,
                checkpoint=_native_checkpointer(check),
                deadline=deadline,
            )
        except UpstreamCheckpointError as error:
            raise error.error from None
        if (
            type(baseline) is not ProductGitDeliveryBaselineV2
            or await _reports(query, root) != before
            or await _configuration(query) != config
            or pinned.observe_index(check) != index
            or reader._root != root
            or reader.contract() != binding
            or git_user_observation_implementation_digest() != implementation
        ):
            raise KernelError("git_user_observation_changed", "Git用户观察期间绑定发生变化")
        await _verify_history(history, session, cancel, deadline, check)
        await _verify_final_git_facts(query, baseline, config)
        await _verify_history(history, session, cancel, deadline, check)
        # 最后一次 await 后仍复核原 Source2 与物理 Index；不承诺跨库原子观察。
        try:
            _verify_final_snapshot(source.workspace, root, _native_checkpointer(check), ports)
        except UpstreamCheckpointError as error:
            raise error.error from None
        if pinned.observe_index(check) != index:
            raise KernelError("git_user_observation_changed", "Git用户观察期间绑定发生变化")
        publication = session._publication
        if publication is None:
            raise KernelError("git_user_observation_host_invalid", "Git用户观察缺少原有效宿主")
        result = _observation(
            baseline,
            pinned,
            index,
            config,
            implementation,
            publication._store_id,
            publication._key_id,
        )
        check()
        return result


async def _verify_history(
    expected: AuthenticatedThreadHistory,
    session: SQLiteSessionStore,
    cancel: CancelToken,
    deadline: float,
    check: Callable[[], None],
) -> None:
    """中段和末段均借原认证Reader复核，观察期间合法事件追加同样拒绝。"""
    current = await session.authenticated_thread_history(
        expected.thread.thread_id, cancel=cancel, deadline=deadline, checkpoint=check
    )
    check()
    if current != expected:
        raise KernelError("git_user_observation_history_changed", "Git用户观察会话历史已经变化")


async def _verify_final_git_facts(
    query: _Queries, baseline: ProductGitDeliveryBaselineV2, config: str
) -> None:
    """先完成末轮异步 Git 观察，再读取末轮认证历史；禁止将旧历史当末端事实。"""
    expected = _Observation(
        baseline.head_oid,
        baseline.head_tree_oid,
        baseline.head_ref,
        baseline.index_observation_sha256,
        baseline.index_observation_bytes,
        baseline.status_sha256,
        baseline.config_names_sha256,
    )
    if await _observe(query) != expected or await _configuration(query) != config:
        raise KernelError("git_user_observation_changed", "Git用户观察期间绑定发生变化")


def _observation(
    baseline: ProductGitDeliveryBaselineV2,
    pinned: PinnedGitUserDirectories,
    index: GitIndexFileObservation,
    config: str,
    implementation: str,
    store_id: UUID,
    key_id: UUID,
) -> ProductGitUserObservation:
    """只组装完整已读事实；原 Session身份元数据不被转换为新的MAC或批准。"""
    facts = git_user_directory_facts(pinned)
    candidate = ProductGitUserObservation.model_construct(
        store_id=store_id,
        key_id=key_id,
        baseline=baseline,
        common_directory_path_sha256=facts["common_directory_path_sha256"],
        common_directory_identity=facts["common_directory_identity"],
        git_directory_path_sha256=facts["git_directory_path_sha256"],
        git_directory_identity=facts["git_directory_identity"],
        index_file_observation=index,
        config_sha256=config,
        implementation_digest=implementation,
        fingerprint="0" * 64,
    )
    return ProductGitUserObservation(
        **candidate.model_dump(exclude={"fingerprint"}),
        fingerprint=product_git_user_observation_fingerprint(candidate),
    )
