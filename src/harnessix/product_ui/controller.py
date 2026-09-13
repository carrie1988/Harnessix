"""以单写者任务编排产品Intent、连接恢复和投影更新。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_service import InteractionService
from harnessix.product_ui.interactions import (
    ACTIVE_TURN_STATES,
    ApprovalEvidence,
    CancelTurnIntent,
    InteractionIntent,
    LoadApprovalEvidenceIntent,
    RespondApprovalIntent,
    RespondQuestionIntent,
    SteerTurnIntent,
    retained_approval_evidence,
)
from harnessix.product_ui.projection import ProductViewState
from harnessix.product_ui.session import (
    ConnectionPhase,
    PreparedClientCommand,
    RecoverableAgentSession,
)
from harnessix.protocol.contracts import MAX_PROTOCOL_TEXT_CHARS, ThreadView
from harnessix.sdk import AgentClient, AgentSDKError

MAX_PRODUCT_THREADS: Final = 1000
MAX_PENDING_INTENTS: Final = 64


class ControllerPhase(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    BROKEN = "broken"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ProductNotice:
    """允许进入UI的稳定、脱敏错误信息。"""

    code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ProductControllerState:
    """TUI消费的不可变工作区级视图，不复制Transcript持久事实。"""

    phase: ControllerPhase = ControllerPhase.CREATED
    workspace: str | None = None
    connection_generation: int = 0
    threads: tuple[ThreadView, ...] = ()
    selected_thread_id: UUID | None = None
    thread_view: ProductViewState | None = None
    approval_evidence: ApprovalEvidence | None = None
    last_notice: ProductNotice | None = None
    revision: int = 0


@dataclass(frozen=True, slots=True)
class StartRequest:
    """固定单个Workspace并可显式指定恢复会话。"""

    workspace: str
    resume_thread_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CreateThreadIntent:
    """为固定Workspace创建并选择一个新Thread。"""

    pass


@dataclass(frozen=True, slots=True)
class SelectThreadIntent:
    """选择列表中已经验证属于当前Workspace的Thread。"""

    thread_id: UUID


@dataclass(frozen=True, slots=True)
class SubmitPromptIntent:
    """向当前无活动Turn的Thread提交一条用户输入。"""

    prompt: str


@dataclass(frozen=True, slots=True)
class RefreshThreadsIntent:
    """重新读取未归档Thread列表，不改变当前领域状态。"""

    pass


@dataclass(frozen=True, slots=True)
class ReconnectIntent:
    """显式建立新连接代际并从持久事实恢复当前Thread。"""

    pass


type ProductIntent = (
    CreateThreadIntent
    | SelectThreadIntent
    | SubmitPromptIntent
    | RefreshThreadsIntent
    | ReconnectIntent
    | LoadApprovalEvidenceIntent
    | RespondApprovalIntent
    | RespondQuestionIntent
    | CancelTurnIntent
    | SteerTurnIntent
)


@dataclass(frozen=True, slots=True)
class CloseReport:
    """区分安全关闭、超时和未结算Intent的有界结果。"""

    clean: bool
    processed_intents: int
    abandoned_intents: int
    error_code: str | None = None


@dataclass(slots=True)
class _QueuedIntent:
    intent: ProductIntent
    completion: asyncio.Future[None]


class _Stop:
    pass


_STOP = _Stop()


def _product_error(error: BaseException) -> ProductUIError:
    if isinstance(error, ProductUIError):
        return error
    if isinstance(error, AgentSDKError):
        return ProductUIError(error.code, error.message, retryable=error.retryable)
    return ProductUIError("controller_internal_failure", "产品控制器发生内部错误")


def _ordered_threads(threads: tuple[ThreadView, ...]) -> tuple[ThreadView, ...]:
    return tuple(
        sorted(threads, key=lambda item: (item.updated_at, str(item.thread_id)), reverse=True)
    )


async def _read_all_threads(session: RecoverableAgentSession) -> tuple[ThreadView, ...]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    values: list[ThreadView] = []
    while True:
        page = await session.list_threads_page(cursor=cursor, limit=200)
        values.extend(page.threads)
        if len(values) > MAX_PRODUCT_THREADS:
            raise ProductUIError("controller_thread_limit", "Workspace会话数量超过客户端上限")
        cursor = page.next_cursor
        if cursor is None:
            return _ordered_threads(tuple(values))
        if cursor in seen_cursors:
            raise ProductUIError("controller_pagination_stalled", "会话列表分页未推进")
        seen_cursors.add(cursor)


async def _select_thread(
    session: RecoverableAgentSession,
    available: tuple[ThreadView, ...],
    thread_id: UUID,
) -> ProductViewState:
    if thread_id not in {thread.thread_id for thread in available}:
        raise ProductUIError("controller_thread_missing", "所选会话不属于当前Workspace")
    await session.resume_thread(thread_id)
    return await session.hydrate_thread(thread_id)


async def _select_optional_thread(
    session: RecoverableAgentSession,
    available: tuple[ThreadView, ...],
    thread_id: UUID | None,
) -> ProductViewState | None:
    return None if thread_id is None else await _select_thread(session, available, thread_id)


async def _execute_interaction(
    service: InteractionService,
    view: ProductViewState | None,
    evidence: ApprovalEvidence | None,
    intent: InteractionIntent,
) -> ProductViewState:
    if isinstance(intent, RespondApprovalIntent):
        return await service.respond_approval(view, evidence, intent)
    if isinstance(intent, RespondQuestionIntent):
        return await service.respond_question(view, intent)
    if isinstance(intent, CancelTurnIntent):
        return await service.cancel_turn(view, intent)
    if isinstance(intent, SteerTurnIntent):
        return await service.steer_turn(view, intent)
    raise ProductUIError("controller_intent_invalid", "领域交互类型无效")


def _resolved_workspace(value: str) -> str:
    try:
        workspace = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProductUIError("product_workspace_invalid", "产品Workspace不可用") from None
    if not workspace.is_dir() or "\0" in str(workspace):
        raise ProductUIError("product_workspace_invalid", "产品Workspace不可用")
    return str(workspace)


def _poll_interval(state: ProductControllerState) -> float:
    turn = state.thread_view.current_turn if state.thread_view else None
    return 0.1 if turn is not None and turn.status in ACTIVE_TURN_STATES else 1.0


class ProductController:
    """串行执行UI Intent，并作为协议I/O与产品状态的唯一任务所有者。"""

    def __init__(self, session: RecoverableAgentSession) -> None:
        self._session = session
        self._interactions = InteractionService(session)
        self._state = ProductControllerState()
        self._intents: asyncio.Queue[_QueuedIntent | _Stop] = asyncio.Queue(
            maxsize=MAX_PENDING_INTENTS
        )
        self._updates: asyncio.Queue[ProductControllerState] = asyncio.Queue(maxsize=1)
        self._actor: asyncio.Task[None] | None = None
        self._accepting = False
        self._processed_intents = 0
        self._close_report: CloseReport | None = None

    @property
    def state(self) -> ProductControllerState:
        return self._state

    def _publish(self, **changes: object) -> ProductControllerState:
        if "thread_view" in changes and "approval_evidence" not in changes:
            changes["approval_evidence"] = retained_approval_evidence(
                cast(ProductViewState | None, changes["thread_view"]),
                self._state.approval_evidence,
            )
        self._state = replace(
            self._state,
            **cast(Any, changes),
            revision=self._state.revision + 1,
        )
        if self._updates.full():
            self._updates.get_nowait()
        self._updates.put_nowait(self._state)
        return self._state

    async def next_update(self) -> ProductControllerState:
        """等待下一份合并后的状态；慢View最多积压一份快照。"""

        return await self._updates.get()

    async def start(self, request: StartRequest) -> ProductControllerState:
        """建立连接并恢复显式或最近选择的Thread，然后启动单写者Actor。"""

        if self._state.phase is not ControllerPhase.CREATED:
            raise ProductUIError("controller_already_started", "产品控制器已经启动")
        workspace = await asyncio.to_thread(_resolved_workspace, request.workspace)
        self._publish(phase=ControllerPhase.STARTING, workspace=workspace, last_notice=None)
        try:
            connection = await self._session.connect()
            threads = await _read_all_threads(self._session)
            saved = self._session.client_state().selected_thread_id
            selected = request.resume_thread_id or saved
            if selected is not None and selected not in {thread.thread_id for thread in threads}:
                if request.resume_thread_id is not None:
                    raise ProductUIError("controller_thread_missing", "恢复会话不属于当前Workspace")
                self._session.clear_selected_thread()
                selected = None
            view = await _select_optional_thread(self._session, threads, selected)
        except Exception as error:
            normalized = _product_error(error)
            self._publish(
                phase=ControllerPhase.BROKEN,
                connection_generation=self._session.connection.generation,
                last_notice=ProductNotice(
                    normalized.code, normalized.message, normalized.retryable
                ),
            )
            raise normalized from None
        self._publish(
            phase=ControllerPhase.READY,
            connection_generation=connection.generation,
            threads=threads,
            selected_thread_id=selected,
            thread_view=view,
            last_notice=None,
        )
        self._accepting = True
        self._actor = asyncio.create_task(self._run(), name="harnessix-product-controller")
        return self._state

    async def dispatch(self, intent: ProductIntent) -> None:
        """排队一个Intent；取消调用者只停止等待，不取消已接纳的领域操作。"""

        if not self._accepting or self._actor is None or self._actor.done():
            raise ProductUIError("controller_not_ready", "产品控制器未接收新操作")
        completion = asyncio.get_running_loop().create_future()
        completion.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )
        try:
            self._intents.put_nowait(_QueuedIntent(intent, completion))
        except asyncio.QueueFull:
            raise ProductUIError("controller_busy", "产品操作队列已满", retryable=True) from None
        await asyncio.shield(completion)

    async def _run(self) -> None:
        while True:
            try:
                queued = await asyncio.wait_for(
                    self._intents.get(), timeout=_poll_interval(self._state)
                )
            except TimeoutError:
                await self._poll_once()
                continue
            if isinstance(queued, _Stop):
                return
            try:
                await self._apply(queued.intent)
            except asyncio.CancelledError:
                if not queued.completion.done():
                    queued.completion.set_exception(
                        ProductUIError(
                            "controller_operation_unknown",
                            "产品操作在关闭时未获得确定结果",
                            retryable=True,
                        )
                    )
                raise
            except Exception as error:
                normalized = _product_error(error)
                phase = (
                    ControllerPhase.BROKEN
                    if self._session.connection.phase is ConnectionPhase.BROKEN
                    else ControllerPhase.READY
                )
                self._publish(
                    phase=phase,
                    connection_generation=self._session.connection.generation,
                    last_notice=ProductNotice(
                        normalized.code, normalized.message, normalized.retryable
                    ),
                )
                queued.completion.set_exception(normalized)
            else:
                self._processed_intents += 1
                queued.completion.set_result(None)

    async def _apply(self, intent: ProductIntent) -> None:
        if isinstance(intent, RefreshThreadsIntent):
            self._publish(threads=await _read_all_threads(self._session), last_notice=None)
            return
        if isinstance(intent, ReconnectIntent):
            connection = await self._session.connect()
            threads = await _read_all_threads(self._session)
            selected = self._state.selected_thread_id
            if selected is not None and selected not in {item.thread_id for item in threads}:
                self._session.clear_selected_thread()
                selected = None
            view = await _select_optional_thread(self._session, threads, selected)
            self._publish(
                phase=ControllerPhase.READY,
                connection_generation=connection.generation,
                threads=threads,
                selected_thread_id=selected,
                thread_view=view,
                last_notice=None,
            )
            return
        if isinstance(intent, CreateThreadIntent):
            await self._create_thread()
            return
        if isinstance(intent, SelectThreadIntent):
            view = await _select_thread(self._session, self._state.threads, intent.thread_id)
            self._publish(
                selected_thread_id=intent.thread_id,
                thread_view=view,
                last_notice=None,
            )
            return
        if isinstance(intent, SubmitPromptIntent):
            await self._submit_prompt(intent.prompt)
            return
        if isinstance(intent, LoadApprovalEvidenceIntent):
            evidence = await self._interactions.load_approval_evidence(
                self._state.thread_view, intent.binding
            )
            self._publish(approval_evidence=evidence, last_notice=None)
            return
        if isinstance(intent, InteractionIntent):
            updated = await _execute_interaction(
                self._interactions,
                self._state.thread_view,
                self._state.approval_evidence,
                intent,
            )
            self._publish(
                threads=await _read_all_threads(self._session),
                thread_view=updated,
                approval_evidence=None,
                last_notice=None,
            )
            return
        raise ProductUIError("controller_intent_invalid", "产品操作类型无效")

    async def _create_thread(self) -> None:
        workspace = self._state.workspace
        assert workspace is not None
        command = self._session.prepare_command()

        async def create(client: AgentClient, prepared: PreparedClientCommand) -> ThreadView:
            return await client.create_thread(workspace, request_id=prepared.request_id)

        thread = await self._session.execute_prepared(command, create)
        threads = await _read_all_threads(self._session)
        view = await self._session.hydrate_thread(thread.thread_id)
        self._publish(
            threads=threads,
            selected_thread_id=thread.thread_id,
            thread_view=view,
            last_notice=None,
        )

    async def _submit_prompt(self, prompt: str) -> None:
        if type(prompt) is not str or not prompt.strip() or len(prompt) > MAX_PROTOCOL_TEXT_CHARS:
            raise ProductUIError("controller_prompt_invalid", "输入内容为空或超过长度上限")
        thread_id = self._state.selected_thread_id
        view = self._state.thread_view
        if thread_id is None or view is None:
            raise ProductUIError("controller_thread_required", "请先创建或选择会话")
        if view.current_turn is not None and view.current_turn.status in ACTIVE_TURN_STATES:
            raise ProductUIError("controller_turn_active", "当前会话已有活动Turn")
        command = self._session.prepare_command()

        async def submit(client: AgentClient, prepared: PreparedClientCommand) -> None:
            await client.start_turn(thread_id, prompt, request_id=prepared.request_id)

        await self._session.execute_prepared(command, submit)
        hydrated = await self._session.hydrate_thread(thread_id)
        self._publish(
            threads=await _read_all_threads(self._session),
            thread_view=hydrated,
            last_notice=None,
        )

    async def _poll_once(self) -> None:
        if self._state.phase is not ControllerPhase.READY:
            return
        thread_id = self._state.selected_thread_id
        if thread_id is None or self._state.thread_view is None:
            return
        try:
            view = await self._session.poll_thread(thread_id, wait_ms=0)
        except Exception as error:
            normalized = _product_error(error)
            phase = (
                ControllerPhase.BROKEN
                if self._session.connection.phase is ConnectionPhase.BROKEN
                else ControllerPhase.READY
            )
            self._publish(
                phase=phase,
                connection_generation=self._session.connection.generation,
                last_notice=ProductNotice(
                    normalized.code, normalized.message, normalized.retryable
                ),
            )
            return
        if view != self._state.thread_view:
            turn = view.current_turn
            threads = (
                self._state.threads
                if turn is not None and turn.status in ACTIVE_TURN_STATES
                else await _read_all_threads(self._session)
            )
            self._publish(threads=threads, thread_view=view, last_notice=None)

    async def close(self, *, deadline_seconds: float = 10.0) -> CloseReport:
        """排空已接纳Intent并有界关闭连接；超时不把命令误报为失败。"""

        if not 0 < deadline_seconds <= 30:
            raise ValueError("关闭时限必须在0到30秒之间")
        if self._state.phase is ControllerPhase.CLOSED:
            assert self._close_report is not None
            return self._close_report
        self._accepting = False
        self._publish(phase=ControllerPhase.CLOSING)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_seconds
        actor = self._actor
        forced = False
        if actor is not None and not actor.done():
            try:
                async with asyncio.timeout(max(0.001, deadline - loop.time())):
                    await self._intents.put(_STOP)
                    await asyncio.shield(actor)
            except TimeoutError:
                forced = True
                actor.cancel()
                await asyncio.gather(actor, return_exceptions=True)
        abandoned = 0
        closed_error = ProductUIError(
            "controller_operation_unknown",
            "产品操作未能在关闭时限内结算",
            retryable=True,
        )
        while not self._intents.empty():
            pending = self._intents.get_nowait()
            if isinstance(pending, _QueuedIntent):
                abandoned += 1
                if not pending.completion.done():
                    pending.completion.set_exception(closed_error)
        error_code = "controller_close_timeout" if forced else None
        try:
            async with asyncio.timeout(max(0.001, deadline - loop.time())):
                await self._session.close()
        except (TimeoutError, AgentSDKError, ProductUIError):
            error_code = error_code or "controller_connection_close_failed"
        clean = error_code is None
        notice = (
            None
            if error_code is None
            else ProductNotice(error_code, "产品客户端未能在时限内安全关闭", True)
        )
        self._publish(
            phase=ControllerPhase.CLOSED,
            last_notice=notice,
        )
        self._close_report = CloseReport(
            clean,
            self._processed_intents,
            abandoned,
            error_code,
        )
        return self._close_report
