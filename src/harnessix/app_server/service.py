"""Headless Agent Protocol服务：执行业务命令、查询与持久交互协调。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid5

from harnessix.agent.models import Budget, ItemDelta, Thread, Turn, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.command_runtime import AgentServiceError as AgentServiceError
from harnessix.app_server.command_runtime import execute_command
from harnessix.app_server.query_runtime import execute_query, poll_events, replay_snapshot
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    ArtifactPageResult,
    ArtifactReadParams,
    CommandParams,
    EventsNextParams,
    EventsNextResult,
    EventsReplayParams,
    EventsReplayResult,
    ProtocolModel,
    PublicItemDelta,
    QuestionRespondParams,
    ThreadArchiveParams,
    ThreadCreateParams,
    ThreadForkParams,
    ThreadGetParams,
    ThreadListParams,
    ThreadListResult,
    ThreadResult,
    ThreadResumeParams,
    TurnCancelParams,
    TurnResult,
    TurnResumeParams,
    TurnRetryParams,
    TurnStartParams,
    TurnSteerParams,
)
from harnessix.protocol.projection import project_thread, project_turn
from harnessix.protocol.requests import ProtocolRequestStore
from harnessix.session.ports import SessionStore


def _budget(value: object) -> Budget | None:
    if value is None:
        return None
    assert isinstance(value, ProtocolModel)
    # 公共JSON保持驼峰；领域模型只接收Python字段名，不能继承协议默认别名。
    return Budget.model_validate(value.model_dump(by_alias=False))


def _resolved_workspace(value: str) -> Path:
    return Path(value).resolve(strict=True)


class AgentApplicationService:
    """公共协议到既有AgentRuntime的薄应用服务，不复制Agent状态机。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        store: SessionStore,
        requests: ProtocolRequestStore,
        artifact_reader: ScopedProtocolArtifactReader | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        if runtime.store is not store:
            raise ValueError("App Server必须绑定Agent Runtime的同一Session")
        if artifact_reader is not None and artifact_reader.session is not store:
            raise ValueError("Artifact Reader必须绑定App Server的同一Session")
        self.runtime = runtime
        self.store = store
        self.requests = requests
        self.artifact_reader = artifact_reader
        self.workspace = None if workspace is None else Path(workspace).resolve(strict=True)
        self._tasks: dict[UUID, asyncio.Task[Turn]] = {}
        self._delta_limit = 1000
        self._deltas: dict[UUID, deque[ItemDelta]] = {}
        self._delta_gaps: set[UUID] = set()
        self._delta_events: dict[UUID, asyncio.Event] = {}
        self._unsubscribe_deltas = runtime.subscribe_deltas(self._receive_delta)
        self._closed = False

    def _receive_delta(self, delta: ItemDelta) -> None:
        buffer = self._deltas.setdefault(delta.thread_id, deque())
        if len(buffer) >= self._delta_limit:
            buffer.popleft()
            self._delta_gaps.add(delta.thread_id)
        buffer.append(delta.model_copy(deep=True))
        self._delta_events.setdefault(delta.thread_id, asyncio.Event()).set()

    def _spawn(self, thread_id: UUID, turn_id: UUID) -> None:
        if self._closed:
            return
        existing = self._tasks.get(turn_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self.runtime.resume_turn(thread_id, turn_id),
            name=f"harnessix-turn-{turn_id}",
        )
        self._tasks[turn_id] = task

        def settled(completed: asyncio.Task[Turn]) -> None:
            if self._tasks.get(turn_id) is completed:
                self._tasks.pop(turn_id, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(settled)

    async def close(self, *, grace_seconds: float = 5.0) -> None:
        """有界等待后台Turn；超时后取消并由Runtime提交确定性取消终态。"""

        if self._closed:
            return
        self._closed = True
        self._unsubscribe_deltas()
        for event in self._delta_events.values():
            event.set()
        tasks = tuple(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _command[Params: CommandParams, Result: ProtocolModel](
        self,
        client_instance_id: UUID,
        method: str,
        params: Params,
        operation: Callable[[], Awaitable[Result]],
        result_model: type[Result],
        *,
        after_result: Callable[[Result], None] | None = None,
    ) -> Result:
        return await execute_command(
            self.runtime,
            self.requests,
            client_instance_id,
            method,
            params,
            operation,
            result_model,
            after_result=after_result,
        )

    async def create_thread(
        self, client_instance_id: UUID, params: ThreadCreateParams
    ) -> ThreadResult:
        async def operation() -> ThreadResult:
            workspace = params.workspace
            if self.workspace is not None:
                try:
                    resolved = await asyncio.to_thread(_resolved_workspace, workspace)
                except (OSError, RuntimeError):
                    raise AgentServiceError(
                        "workspace_not_configured", "Thread Workspace不属于当前产品运行时"
                    ) from None
                # Path在Windows上按大小写不敏感规则比较；POSIX仍保持精确路径身份。
                if resolved != self.workspace:
                    raise AgentServiceError(
                        "workspace_not_configured", "Thread Workspace不属于当前产品运行时"
                    )
                workspace = str(resolved)
            identity = uuid5(
                client_instance_id,
                f"harnessix.protocol-thread/v1:{params.request_id}",
            )
            thread = await self.runtime.create_thread(workspace, thread_id=identity)
            return ThreadResult(thread=project_thread(thread))

        return await self._command(
            client_instance_id, "thread/create", params, operation, ThreadResult
        )

    async def get_thread(self, params: ThreadGetParams) -> ThreadResult:
        async def operation() -> ThreadResult:
            return ThreadResult(
                thread=project_thread(await self.store.get_thread(params.thread_id))
            )

        return await execute_query(self.runtime, params, operation)

    async def list_threads(self, params: ThreadListParams) -> ThreadListResult:
        async def operation() -> ThreadListResult:
            after: UUID | None = None
            if params.cursor is not None:
                try:
                    after = UUID(params.cursor)
                except ValueError:
                    raise AgentServiceError("invalid_cursor", "Thread列表游标无效") from None
            selected, has_more = await self.store.list_thread_page(
                after=after, archived=params.archived, limit=params.limit
            )
            next_cursor = str(selected[-1].thread_id) if has_more else None
            return ThreadListResult(
                threads=tuple(project_thread(thread) for thread in selected),
                next_cursor=next_cursor,
            )

        return await execute_query(self.runtime, params, operation)

    async def resume_thread(self, params: ThreadResumeParams) -> ThreadResult:
        thread: Thread | None = None

        async def operation() -> ThreadResult:
            nonlocal thread
            thread = await self.runtime.resume_thread(params.thread_id)
            return ThreadResult(thread=project_thread(thread))

        def drive(_result: ThreadResult) -> None:
            # 原聚合决定恢复对象；完整公开DTO检查通过前不得调度后台Turn。
            assert thread is not None
            if thread.active_turn_id is not None:
                turn = next(item for item in thread.turns if item.turn_id == thread.active_turn_id)
                if turn.status in {
                    TurnStatus.ACCEPTED,
                    TurnStatus.EXECUTING_TOOLS,
                    TurnStatus.WAITING_APPROVAL,
                    TurnStatus.WAITING_ACTION,
                }:
                    if self._closed:
                        raise AgentServiceError("server_closing", "服务端正在关闭")
                    self._spawn(thread.thread_id, turn.turn_id)

        return await execute_query(self.runtime, params, operation, after_result=drive)

    async def fork_thread(self, client_instance_id: UUID, params: ThreadForkParams) -> ThreadResult:
        async def operation() -> ThreadResult:
            thread = await self.runtime.fork_thread(
                params.source_thread_id,
                request_id=params.request_id,
                through_turn_id=params.through_turn_id,
            )
            return ThreadResult(thread=project_thread(thread))

        return await self._command(
            client_instance_id, "thread/fork", params, operation, ThreadResult
        )

    async def archive_thread(
        self, client_instance_id: UUID, params: ThreadArchiveParams
    ) -> ThreadResult:
        async def operation() -> ThreadResult:
            thread = await self.runtime.archive_thread(params.thread_id, reason=params.reason)
            return ThreadResult(thread=project_thread(thread))

        return await self._command(
            client_instance_id, "thread/archive", params, operation, ThreadResult
        )

    async def start_turn(self, client_instance_id: UUID, params: TurnStartParams) -> TurnResult:
        async def operation() -> TurnResult:
            turn = await self.runtime.accept_turn(
                params.thread_id,
                params.prompt,
                request_id=params.request_id,
                budget=_budget(params.budget),
            )
            return TurnResult(turn=project_turn(turn))

        def drive(result: TurnResult) -> None:
            if result.turn.status == TurnStatus.ACCEPTED.value:
                self._spawn(params.thread_id, result.turn.turn_id)

        return await self._command(
            client_instance_id,
            "turn/start",
            params,
            operation,
            TurnResult,
            after_result=drive,
        )

    async def retry_turn(self, client_instance_id: UUID, params: TurnRetryParams) -> TurnResult:
        async def operation() -> TurnResult:
            turn = await self.runtime.accept_retry_turn(
                params.thread_id,
                params.source_turn_id,
                request_id=params.request_id,
                budget=_budget(params.budget),
            )
            return TurnResult(turn=project_turn(turn))

        def drive(result: TurnResult) -> None:
            if result.turn.status == TurnStatus.ACCEPTED.value:
                self._spawn(params.thread_id, result.turn.turn_id)

        return await self._command(
            client_instance_id,
            "turn/retry",
            params,
            operation,
            TurnResult,
            after_result=drive,
        )

    async def resume_turn(self, client_instance_id: UUID, params: TurnResumeParams) -> TurnResult:
        async def operation() -> TurnResult:
            return TurnResult(
                turn=project_turn(await self.runtime.resume_turn(params.thread_id, params.turn_id))
            )

        return await self._command(client_instance_id, "turn/resume", params, operation, TurnResult)

    async def cancel_turn(self, client_instance_id: UUID, params: TurnCancelParams) -> TurnResult:
        async def operation() -> TurnResult:
            return TurnResult(
                turn=project_turn(await self.runtime.cancel(params.thread_id, params.turn_id))
            )

        return await self._command(client_instance_id, "turn/cancel", params, operation, TurnResult)

    async def steer_turn(self, client_instance_id: UUID, params: TurnSteerParams) -> TurnResult:
        async def operation() -> TurnResult:
            turn = await self.runtime.steer_turn(
                params.thread_id,
                params.turn_id,
                params.text,
                request_id=params.request_id,
            )
            return TurnResult(turn=project_turn(turn))

        return await self._command(
            client_instance_id,
            "turn/steer",
            params,
            operation,
            TurnResult,
        )

    async def respond_approval(
        self, client_instance_id: UUID, params: ApprovalRespondParams
    ) -> TurnResult:
        async def operation() -> TurnResult:
            decision = ApprovalDecision(
                outcome=ApprovalOutcome(params.decision.outcome),
                actor=params.decision.actor,
                reason=params.decision.reason,
            )
            turn = await self.runtime.reply_approval(
                params.thread_id,
                params.turn_id,
                params.approval_id,
                fingerprint=params.fingerprint,
                decision=decision,
            )
            return TurnResult(turn=project_turn(turn))

        def drive(result: TurnResult) -> None:
            if result.turn.status in {
                TurnStatus.EXECUTING_TOOLS.value,
                TurnStatus.WAITING_APPROVAL.value,
            }:
                self._spawn(params.thread_id, result.turn.turn_id)

        return await self._command(
            client_instance_id,
            "approval/respond",
            params,
            operation,
            TurnResult,
            after_result=drive,
        )

    async def respond_question(
        self, client_instance_id: UUID, params: QuestionRespondParams
    ) -> TurnResult:
        async def operation() -> TurnResult:
            turn = await self.runtime.reply_question(
                params.thread_id,
                params.turn_id,
                params.question_id,
                answer=params.answer,
            )
            return TurnResult(turn=project_turn(turn))

        def drive(result: TurnResult) -> None:
            if result.turn.status == TurnStatus.EXECUTING_TOOLS.value:
                self._spawn(params.thread_id, result.turn.turn_id)

        return await self._command(
            client_instance_id,
            "question/respond",
            params,
            operation,
            TurnResult,
            after_result=drive,
        )

    async def replay_events(self, params: EventsReplayParams) -> EventsReplayResult:
        return await execute_query(
            self.runtime, params, lambda: replay_snapshot(self.store, params)
        )

    async def read_artifact(self, params: ArtifactReadParams) -> ArtifactPageResult:
        async def operation() -> ArtifactPageResult:
            if self.artifact_reader is None:
                raise AgentServiceError("artifact_not_enabled", "App Server未配置Artifact读取端口")
            return await self.artifact_reader.read(
                params.thread_id, params.artifact_id, offset=params.offset, limit=params.limit
            )

        return await execute_query(self.runtime, params, operation)

    def _take_deltas(
        self, thread_id: UUID, limit: int
    ) -> tuple[tuple[PublicItemDelta, ...], bool, bool]:
        buffer = self._deltas.get(thread_id)
        selected: list[ItemDelta] = []
        if buffer is not None:
            while buffer and len(selected) < limit:
                selected.append(buffer.popleft())
            if not buffer:
                self._deltas.pop(thread_id, None)
        gap = thread_id in self._delta_gaps
        self._delta_gaps.discard(thread_id)
        return (
            tuple(PublicItemDelta.model_validate(delta.model_dump()) for delta in selected),
            bool(buffer),
            gap,
        )

    async def _next_snapshot(
        self, params: EventsNextParams, *, include_deltas: bool
    ) -> EventsNextResult | None:
        replay = await replay_snapshot(
            self.store,
            EventsReplayParams(
                thread_id=params.thread_id,
                after_cursor=params.after_cursor,
                limit=params.limit,
            ),
        )
        if replay.scanned_through > params.after_cursor or replay.has_more:
            return EventsNextResult(replay=replay)
        if not include_deltas:
            return None
        deltas, has_more, gap = self._take_deltas(params.thread_id, params.limit)
        if deltas or gap:
            return EventsNextResult(
                replay=replay,
                deltas=deltas,
                live_has_more=has_more,
                live_gap=gap,
            )
        return None

    async def next_events(
        self, params: EventsNextParams, *, include_deltas: bool = True
    ) -> EventsNextResult:
        """先准入查询再注册信号；最终原Replay/Delta统一保护，Delta仍为可丢失显示。"""
        if type(include_deltas) is not bool:
            raise AgentServiceError("invalid_params", "协议参数无效")

        async def operation() -> EventsNextResult:
            signal = self._delta_events.setdefault(params.thread_id, asyncio.Event())
            return await poll_events(
                params,
                signal=signal,
                snapshot=lambda: self._next_snapshot(params, include_deltas=include_deltas),
                replay=lambda: replay_snapshot(
                    self.store,
                    EventsReplayParams(
                        thread_id=params.thread_id,
                        after_cursor=params.after_cursor,
                        limit=params.limit,
                    ),
                ),
                closed=lambda: self._closed,
            )

        return await execute_query(self.runtime, params, operation)
