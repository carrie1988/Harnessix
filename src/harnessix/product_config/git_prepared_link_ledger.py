"""实际待审批 Git 关联的原认证事务写入与只读回读；调用方拥有提交和回滚。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

from harnessix.agent.cancellation import CancelToken, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.product_config.git_authentication_control import GitAuthenticationControl
from harnessix.product_config.git_baseline import _BASELINE_TIMEOUT_SECONDS
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_delivery_review_host import require_git_review_host
from harnessix.product_config.git_prefix_rows import GitPrefixRows, capture_git_prefix_rows
from harnessix.product_config.git_prefix_sql import (
    git_prefix_sql_window,
    git_prefix_transaction_epoch,
    require_git_prefix_transaction_epoch,
)
from harnessix.product_config.git_prefix_writer import (
    begin_git_prefix_write,
    publish_git_prefix_changes,
)
from harnessix.product_config.git_prepared_link_connection import (
    _prepared_git_connection_lifecycle_observer,
    _prepared_git_connection_observer,
)
from harnessix.product_config.git_prepared_link_contracts import ProductGitPreparedLink
from harnessix.product_config.git_prepared_link_observation import (
    PreparedLinkReadSet,
    observe_prepared_state,
)
from harnessix.product_config.git_prepared_link_proof import (
    PreparedLinkEvidence,
    authenticate_prepared_link,
    prepared_link_changed,
)
from harnessix.product_config.git_prepared_link_rows import (
    prepared_link_columns,
    read_prepared_link_rows,
)
from harnessix.product_config.git_prepared_link_wire import encode_product_git_prepared_link
from harnessix.product_config.git_prepared_runtime_thread import (
    _prepared_runtime_thread_observer,
    require_prepared_git_runtime_thread,
)
from harnessix.product_config.git_user_observation import verify_product_git_user_observation
from harnessix.session.git_publication_contracts import GitDeliveryRecordClaims
from harnessix.tools.git import GitReadRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


class ProductGitPreparedLinkLedger:
    """仅消费同一原活跃宿主；prepared 回读不能用于追认批准、执行或完整恢复。"""

    def __init__(
        self,
        database: sqlite3.Connection,
        router: TrustedActionRouter,
        core_store: ProductGitDeliveryCoreStore,
        artifacts: SQLiteArtifactStore,
        reader: GitReadRuntime,
        *,
        snapshot_ports: WorkspaceSnapshotPorts,
        workspace_scope: str,
    ) -> None:
        self._database, self._router, self._core_store = database, router, core_store
        self._artifacts, self._reader = artifacts, reader
        self._ports, self._workspace_scope = snapshot_ports, workspace_scope

    async def prepare(
        self, route_id: UUID, *, cancel: CancelToken, checkpoint: Callable[[], None]
    ) -> ProductGitPreparedLink:
        """在调用方已有写事务内发布原关联；成功仍未 COMMIT，异常必须由调用方回滚。"""
        await asyncio.sleep(0)
        budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
        read_set = PreparedLinkReadSet()
        with _control(self, cancel, budget, checkpoint, read_set) as check:
            timeout = asyncio.timeout(budget.remaining())
            try:
                async with timeout:
                    result = await _prepare(self, route_id, cancel, budget, check, read_set)
                    check()
                    return result
            except TimeoutError:
                if timeout.expired():
                    raise KernelError("git_process_timeout", "Git业务关联总期限已耗尽") from None
                raise

    async def read_all(
        self, *, cancel: CancelToken, checkpoint: Callable[[], None]
    ) -> tuple[ProductGitPreparedLink, ...]:
        """已有只读事务中全前缀及所有关联逐个验真，不修复、不补签、不忽略坏关联。"""
        await asyncio.sleep(0)
        budget = GitOperationBudget(_BASELINE_TIMEOUT_SECONDS)
        read_set = PreparedLinkReadSet()
        with _control(self, cancel, budget, checkpoint, read_set) as check:
            timeout = asyncio.timeout(budget.remaining())
            try:
                async with timeout:
                    result, _rows = await _read_all(self, cancel, budget, check, read_set)
                    check()
                    return result
            except TimeoutError:
                if timeout.expired():
                    raise KernelError("git_process_timeout", "Git业务关联总期限已耗尽") from None
                raise


@contextmanager
def _control(
    ledger: ProductGitPreparedLinkLedger,
    cancel: CancelToken,
    budget: GitOperationBudget,
    checkpoint: Callable[[], None],
    read_set: PreparedLinkReadSet,
) -> Iterator[Callable[[], None]]:
    """专用 SQL 合作窗口冻结原宿主、文件及事务代际；不覆盖共享 Session 回调。"""
    if type(cancel) is not CancelToken or type(ledger._database) is not sqlite3.Connection:
        raise prepared_link_changed()
    host = require_git_review_host(
        ledger._router,
        ledger._core_store,
        ledger._artifacts,
        ledger._reader,
        ledger._ports,
        ledger._workspace_scope,
    )
    references = tuple(vars(ledger).values())
    database, state = ledger._database, ledger._artifacts.session.path.parent
    path = state / "git-delivery" / "git-delivery.db"
    observe_connection = _prepared_git_connection_observer(database, path)
    observe_lifecycle = _prepared_git_connection_lifecycle_observer(database)
    observe_thread = _prepared_runtime_thread_observer(database, ledger._router, ledger._artifacts)

    with (
        read_set.source_scope,
        observe_prepared_state(ledger._router, ledger._core_store, ledger._artifacts) as unchanged,
    ):
        epoch: tuple[object, int] | None = None

        def internal() -> None:
            cancel.checkpoint()
            budget.remaining()
            host()
            # 受管U验证子Task只复核来源；SQL消费与发布仍由原Task窗口准入。
            observe_connection()
            observe_thread()
            unchanged()
            if any(a is not b for a, b in zip(vars(ledger).values(), references, strict=True)):
                raise prepared_link_changed()
            if epoch is not None:
                require_git_prefix_transaction_epoch(database, epoch)

        internal = parent_cancel_checkpointer(internal)

        def local_check() -> None:
            # 纯段不访问 SQL/文件/Owner；不缓存认证结果，边界仍走完整 internal。
            cancel.checkpoint()
            budget.remaining()
            observe_lifecycle()
            observe_thread()
            if any(a is not b for a, b in zip(vars(ledger).values(), references, strict=True)):
                raise prepared_link_changed()
            if epoch is not None:
                require_git_prefix_transaction_epoch(database, epoch)

        local_check = parent_cancel_checkpointer(local_check)

        def authenticate() -> None:
            internal()
            checkpoint()
            internal()

        control = GitAuthenticationControl(local_check, authenticate)
        control()
        if not database.in_transaction:
            raise KernelError("git_delivery_store_transaction_required", "Git业务关联需要已有事务")
        with git_prefix_sql_window(database, checkpoint=control):
            epoch = git_prefix_transaction_epoch(database)

            yield control
            control()
        # 原 SQL finally 的末次宿主回调之后不再 await 或调用外部回调。
        epoch = None
        terminal = GitAuthenticationControl(local_check, internal)
        with git_prefix_sql_window(database, checkpoint=terminal):
            epoch = git_prefix_transaction_epoch(database)
            read_set.require_sql(database, terminal)
            read_set.terminal(
                ledger._router,
                ledger._core_store,
                ledger._artifacts,
                ledger._ports,
                ledger._workspace_scope,
                terminal,
            )
            read_set.require_sql(database, terminal)
            terminal()
        epoch = None


async def _authenticate(
    ledger: ProductGitPreparedLinkLedger,
    route_id: UUID,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    read_set: PreparedLinkReadSet,
) -> PreparedLinkEvidence:
    """原 pending Proof 后只读复核完整 U；复用同次控制，不重捕获或产生批准。"""
    evidence = await authenticate_prepared_link(
        route_id,
        ledger._router,
        ledger._core_store,
        ledger._artifacts,
        ledger._ports,
        ledger._workspace_scope,
        cancel=cancel,
        budget=budget,
        checkpoint=check,
    )
    await verify_product_git_user_observation(
        evidence.link.plan.core.user_observation,
        evidence.history,
        ledger._router,
        ledger._core_store.store,
        ledger._reader,
        session=ledger._artifacts.session,
        cancel=cancel,
        budget=budget,
        checkpoint=check,
        snapshot_ports=ledger._ports,
        source_scope=read_set.source_scope,
    )
    check()
    return evidence


async def _read_all(
    ledger: ProductGitPreparedLinkLedger,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    read_set: PreparedLinkReadSet,
) -> tuple[tuple[ProductGitPreparedLink, ...], GitPrefixRows]:
    """全物理前缀先验真，全部关联再交叉回读；跨 await 后完整行必须保持。"""
    publication = ledger._artifacts.session._publication
    assert publication is not None
    changes = ledger._database.total_changes
    anchor = ledger._database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    links, rows = read_prepared_link_rows(ledger._database, publication, checkpoint=check)
    for link in links:
        actual = await _authenticate(
            ledger, link.plan.route.execution.plan_id, cancel, budget, check, read_set
        )
        check()
        if actual.link != link:
            raise prepared_link_changed()
        read_set.evidence[link.plan.route.execution.plan_id] = actual
    if changes != ledger._database.total_changes or (
        anchor != ledger._database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    ):
        raise prepared_link_changed()
    if capture_git_prefix_rows(ledger._database, checkpoint=check) != rows:
        raise prepared_link_changed()
    check()
    if changes != ledger._database.total_changes or (
        anchor != ledger._database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    ):
        raise prepared_link_changed()
    read_set.complete(rows, anchor, changes)
    return links, rows


async def _prepare(
    ledger: ProductGitPreparedLinkLedger,
    route_id: UUID,
    cancel: CancelToken,
    budget: GitOperationBudget,
    check: Callable[[], None],
    read_set: PreparedLinkReadSet,
) -> ProductGitPreparedLink:
    """先核对全集和原同身份，再新增一条 prepared；失败留给原事务回滚。"""
    database = ledger._database
    if database.execute("PRAGMA query_only").fetchone() == (1,):
        raise KernelError("git_delivery_store_read_only", "Git待审批只读账本不接受发布")
    database.execute("UPDATE main.git_delivery_metadata SET value=value WHERE 0")
    existing, rows = await _read_all(ledger, cancel, budget, check, read_set)
    changes = database.total_changes
    anchor = database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    evidence = await _authenticate(ledger, route_id, cancel, budget, check, read_set)
    link = evidence.link
    require_prepared_git_runtime_thread(database, link.plan.core.thread_id)
    read_set.evidence[route_id] = evidence
    if (
        changes != database.total_changes
        or anchor != database.execute("SELECT * FROM git_prefix_anchor").fetchone()
    ):
        raise prepared_link_changed()
    if capture_git_prefix_rows(database, checkpoint=check) != rows:
        raise prepared_link_changed()
    for prior in existing:
        if prior.plan.route.execution.plan_id == route_id:
            if prior != link:
                raise prepared_link_changed()
            return prior
    publication = ledger._artifacts.session._publication
    assert publication is not None
    window = begin_git_prefix_write(
        database, publication.git, publication.git_prefix, checkpoint=check
    )
    body = encode_product_git_prepared_link(link, checkpoint=check)
    columns = prepared_link_columns(link, body)
    database.execute("INSERT INTO git_product_links VALUES (?,?,?,?,?,?,?,?,?,?,?)", columns)
    database.execute(
        "INSERT INTO git_product_link_events VALUES (?,?,?,?)",
        (str(route_id), 0, "prepared", columns[-1]),
    )
    core = link.plan.core
    claims = GitDeliveryRecordClaims(
        record_kind="product_link",
        record_id=route_id,
        publication_epoch=uuid4(),
        sequence=1,
        previous_sha256="0" * 64,
        delivery_id=core.delivery_id,
        thread_id=core.thread_id,
        turn_id=core.turn_id,
        call_id=core.call.call_id,
        route_id=route_id,
    )
    await publish_git_prefix_changes(
        window,
        (claims,),
        publication.git,
        publication.git_prefix,
        publication._events._protection,
        cancel=cancel,
    )
    check()
    checked, _rows = await _read_all(ledger, cancel, budget, check, read_set)
    if link not in checked:
        raise prepared_link_changed()
    return link
