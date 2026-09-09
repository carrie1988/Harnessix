from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid5

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, ItemDelta, Turn, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
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
    validate_protocol_input,
)
from harnessix.protocol.projection import project_replay, project_thread, project_turn
from harnessix.protocol.requests import ProtocolRequestError, ProtocolRequestStore
from harnessix.session.ports import SessionStore


class AgentServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _budget(value: object) -> Budget | None:
    if value is None:
        return None
    assert isinstance(value, ProtocolModel)
    return Budget.model_validate(value.model_dump())


class AgentApplicationService:
    """公共协议到既有AgentRuntime的薄应用服务，不复制Agent状态机。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        store: SessionStore,
        requests: ProtocolRequestStore,
        artifact_reader: ScopedProtocolArtifactReader | None = None,
    ) -> None:
        self.runtime = runtime
        self.store = store
        self.requests = requests
        self.artifact_reader = artifact_reader
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

    @staticmethod
    def _raise(error: KernelError | ProtocolRequestError) -> AgentServiceError:
        return AgentServiceError(
            error.code,
            str(error),
            retryable=isinstance(error, KernelError) and error.retryable,
        )

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
        wire = params.model_dump(mode="json", by_alias=True)
        try:
            claim = await self.requests.claim(
                client_instance_id,
                params.request_id,
                method,
                wire,
            )
            if claim.record.state == "completed":
                result = validate_protocol_input(result_model, claim.record.outcome)
                if after_result is not None:
                    after_result(result)
                return result
            if claim.record.state == "failed":
                outcome = claim.record.outcome
                if isinstance(outcome, dict):
                    raise AgentServiceError(
                        str(outcome.get("code", "command_failed")),
                        str(outcome.get("message", "协议命令失败")),
                        retryable=outcome.get("retryable") is True,
                    )
                raise AgentServiceError("command_failed", "协议命令失败")
            result = await operation()
            await self.requests.complete(
                client_instance_id,
                params.request_id,
                result.model_dump(mode="json", by_alias=True),
            )
            if after_result is not None:
                after_result(result)
            return result
        except (KernelError, ProtocolRequestError) as error:
            service_error = self._raise(error)
            if error.code != "idempotency_conflict":
                try:
                    await self.requests.fail(
                        client_instance_id,
                        params.request_id,
                        {
                            "code": service_error.code,
                            "message": service_error.message,
                            "retryable": service_error.retryable,
                        },
                    )
                except ProtocolRequestError:
                    pass
            raise service_error from None

    async def create_thread(
        self, client_instance_id: UUID, params: ThreadCreateParams
    ) -> ThreadResult:
        async def operation() -> ThreadResult:
            identity = uuid5(
                client_instance_id,
                f"harnessix.protocol-thread/v1:{params.request_id}",
            )
            thread = await self.runtime.create_thread(params.workspace, thread_id=identity)
            return ThreadResult(thread=project_thread(thread))

        return await self._command(
            client_instance_id, "thread/create", params, operation, ThreadResult
        )

    async def get_thread(self, params: ThreadGetParams) -> ThreadResult:
        return ThreadResult(thread=project_thread(await self.store.get_thread(params.thread_id)))

    async def list_threads(self, params: ThreadListParams) -> ThreadListResult:
        ids = sorted(await self.store.thread_ids(), key=str)
        if params.cursor is not None:
            try:
                after = UUID(params.cursor)
            except ValueError:
                raise AgentServiceError("invalid_cursor", "Thread列表游标无效") from None
            ids = [thread_id for thread_id in ids if str(thread_id) > str(after)]
        threads = [await self.store.get_thread(thread_id) for thread_id in ids]
        if params.archived is not None:
            threads = [
                thread for thread in threads if (thread.archive is not None) is params.archived
            ]
        selected = threads[: params.limit]
        next_cursor = (
            str(selected[-1].thread_id) if len(threads) > len(selected) and selected else None
        )
        return ThreadListResult(
            threads=tuple(project_thread(thread) for thread in selected),
            next_cursor=next_cursor,
        )

    async def resume_thread(self, params: ThreadResumeParams) -> ThreadResult:
        thread = await self.runtime.resume_thread(params.thread_id)
        if thread.active_turn_id is not None:
            turn = next(item for item in thread.turns if item.turn_id == thread.active_turn_id)
            if turn.status in {
                TurnStatus.ACCEPTED,
                TurnStatus.EXECUTING_TOOLS,
                TurnStatus.WAITING_APPROVAL,
                TurnStatus.WAITING_ACTION,
            }:
                self._spawn(thread.thread_id, turn.turn_id)
        return ThreadResult(thread=project_thread(thread))

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
        events = await self.store.events(params.thread_id, after=params.after_cursor)
        page = events[: params.limit]
        scanned = page[-1].sequence if page else params.after_cursor
        return project_replay(
            params.thread_id,
            page,
            scanned_through=scanned,
            has_more=len(events) > len(page),
        )

    async def read_artifact(self, params: ArtifactReadParams) -> ArtifactPageResult:
        if self.artifact_reader is None:
            raise AgentServiceError("artifact_not_enabled", "App Server未配置Artifact读取端口")
        return await self.artifact_reader.read(
            params.thread_id,
            params.artifact_id,
            offset=params.offset,
            limit=params.limit,
        )

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
        replay = await self.replay_events(
            EventsReplayParams(
                thread_id=params.thread_id,
                after_cursor=params.after_cursor,
                limit=params.limit,
            )
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
        """长轮询公开事件；Replay是事实，Delta只提供可丢失的低延迟显示。"""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + params.wait_ms / 1000
        signal = self._delta_events.setdefault(params.thread_id, asyncio.Event())
        while True:
            ready = await self._next_snapshot(params, include_deltas=include_deltas)
            if ready is not None:
                return ready
            remaining = deadline - loop.time()
            if remaining <= 0 or self._closed:
                break
            signal.clear()
            # clear与wait之间重新读取，避免Delta恰在注册窗口到达而沉睡；
            # 50ms轮询用于观察不产生Delta的审批、提问、Tool和终态事件。
            ready = await self._next_snapshot(params, include_deltas=include_deltas)
            if ready is not None:
                return ready
            try:
                async with asyncio.timeout(min(0.05, remaining)):
                    await signal.wait()
            except TimeoutError:
                pass
        ready = await self._next_snapshot(params, include_deltas=include_deltas)
        if ready is not None:
            return ready
        replay = await self.replay_events(
            EventsReplayParams(
                thread_id=params.thread_id,
                after_cursor=params.after_cursor,
                limit=params.limit,
            )
        )
        if replay.scanned_through > params.after_cursor or replay.has_more:
            return EventsNextResult(replay=replay)
        return EventsNextResult(replay=replay, timed_out=True)
