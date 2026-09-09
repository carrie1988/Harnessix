from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid5

from harnessix.agent.errors import KernelError
from harnessix.agent.models import Budget, Turn, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    CommandParams,
    EventsReplayParams,
    EventsReplayResult,
    ProtocolModel,
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
    ) -> None:
        self.runtime = runtime
        self.store = store
        self.requests = requests
        self._tasks: dict[UUID, asyncio.Task[Turn]] = {}

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

        tasks = tuple(self._tasks.values())
        if not tasks:
            return
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
            if turn.status == TurnStatus.ACCEPTED:
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

        return await self._command(
            client_instance_id,
            "approval/respond",
            params,
            operation,
            TurnResult,
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
