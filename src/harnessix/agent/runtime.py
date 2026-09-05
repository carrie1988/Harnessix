from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, aclosing
from dataclasses import replace
from types import TracebackType
from typing import Self
from uuid import UUID

from harnessix.agent import batch_patching
from harnessix.agent.approvals import (
    approval_for,
    approval_matches,
    remaining_seconds,
    request_fingerprint,
    tool_fingerprint,
)
from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.execution import ToolExecutionScope
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    TERMINAL_TURNS,
    AgentFailure,
    ApprovalContent,
    ApprovalRequestContent,
    Budget,
    ErrorContent,
    EventDraft,
    EventPayload,
    ItemDelta,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PatchApprovalRequestContent,
    PatchBatchApprovalRequestContent,
    ProcessApprovalRequestContent,
    TextContent,
    Thread,
    ThreadCreated,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.agent.patching import execution_approval, inspection_scope, result_content
from harnessix.agent.ports import (
    NoTools,
    PatchBatchRuntime,
    PatchRuntime,
    ScopedToolRuntime,
    ToolRuntime,
)
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.agent.telemetry import KernelTelemetry
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.artifacts.ports import ArtifactPublisher, BatchDiffPublisher
from harnessix.domain.models import (
    ActionContext,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRecord,
    EffectClass,
    TraceContext,
    utc_now,
)
from harnessix.models.contracts import (
    ModelProvider,
    ModelRequest,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.observability.core import NoOpObservability, Observability
from harnessix.session.ports import SessionStore
from harnessix.tools.runtime import _drain


class AgentRuntime:
    """进程内 Kernel 宿主；不承担 CLI、模型 SDK、Shell 或 Sandbox 职责。"""

    def __init__(
        self,
        store: SessionStore,
        provider: ModelProvider,
        tools: ToolRuntime | None = None,
        *,
        scoped_tools: ScopedToolRuntime | None = None,
        patches: PatchRuntime | None = None,
        patch_batches: PatchBatchRuntime | None = None,
        artifacts: ArtifactPublisher | None = None,
        batch_diffs: BatchDiffPublisher | None = None,
        on_delta: Callable[[ItemDelta], None] | None = None,
        observability: Observability | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if tools is not None and scoped_tools is not None:
            raise KernelError("tool_runtime_conflict", "旧工具入口与 Scoped 入口不能同时配置")
        if artifacts is not None and (artifacts.session is not store or scoped_tools is None):
            raise KernelError(
                "artifact_store_mismatch", "Artifact 发布器必须绑定同一 Session 和 Scoped 入口"
            )
        if batch_diffs is not None and (
            batch_diffs.session is not store or batch_diffs.bridge is not patch_batches
        ):
            raise KernelError("artifact_store_mismatch", "差异发布必须绑定原 Session 和整组端口")
        self._batch_diffs = batch_diffs
        self._artifacts = artifacts
        self.store = store
        self._telemetry = KernelTelemetry(observability or NoOpObservability())
        self.provider = provider
        self._legacy_tools = tools if tools is not None else NoTools()
        self._scoped_tools = scoped_tools
        self.tools = scoped_tools if scoped_tools is not None else self._legacy_tools
        definitions = self.tools.definitions()
        self._patches = patches
        if patches is not None:
            definition = patches.definition()
            if (
                definition.name != "apply_patch"
                or definition.effect_class != EffectClass.NON_IDEMPOTENT_WRITE
                or not definition.requires_approval
                or not definition.requires_idempotency
                or not definition.supports_reconciliation
            ):
                raise KernelError(
                    "patch_contract_invalid", "专用 Patch 入口必须声明一次性写入、审批和核对"
                )
            definitions = (*definitions, definition)
        self._patch_batches = patch_batches
        if patch_batches is not None:
            definition = patch_batches.definition()
            if (
                definition.name != "apply_patch_batch"
                or definition.effect_class != EffectClass.NON_IDEMPOTENT_WRITE
                or not definition.requires_approval
                or not definition.requires_idempotency
                or not definition.supports_reconciliation
            ):
                raise KernelError(
                    "patch_batch_contract_invalid", "整组专用端口必须声明一次性写、审批和核对"
                )
            definitions = (*definitions, definition)
        if len({d.name for d in definitions}) != len(definitions):
            raise KernelError("duplicate_tool", "Tool 名称重复")
        self._definitions = {d.name: d.model_copy(deep=True) for d in definitions}
        self._on_delta = on_delta or (lambda _: None)
        self._fault = fault or (lambda _: None)
        self._owner: AbstractAsyncContextManager[None] | None = None
        self._open = False
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._active: dict[UUID, tuple[UUID, CancelToken, asyncio.Task[object]]] = {}

    async def __aenter__(self) -> Self:
        if self._owner is not None:
            raise KernelError("runtime_open", "Runtime 不能重复打开")
        owner = self.store.runtime_owner()
        await owner.__aenter__()
        self._owner = owner
        self._open = True
        try:
            await self.store.initialize()
            for thread_id in await self.store.thread_ids():
                thread = await self.store.get_thread(thread_id)
                if thread.active_turn_id is not None:
                    await self._recover(thread)
        except BaseException:
            self._open = False
            self._owner = None
            await owner.__aexit__(None, None, None)
            raise
        return self

    async def _recover(self, thread: Thread) -> None:
        assert thread.active_turn_id is not None
        turn = get_turn(thread, thread.active_turn_id)
        with self._telemetry.operation(
            "recovery",
            thread_id=thread.thread_id,
            turn_id=turn.turn_id,
            trace_context=turn.trace_context,
        ) as operation:
            # Process Action 的Effect Journal仍是唯一执行事实；b2b2只负责保存等待边界，
            # 在b2c接入核对驱动前，启动恢复不得把仍在运行的Action误判为进程中断。
            if turn.status == TurnStatus.WAITING_ACTION:
                operation.finish(turn.status.value)
                return
            if turn.status == TurnStatus.WAITING_APPROVAL:
                if remaining_seconds(turn) > 0:
                    operation.finish(turn.status.value)
                    return
                recovered = await self._finish(
                    thread.thread_id,
                    turn.turn_id,
                    TurnStatus.FAILED,
                    AgentFailure(code="time_budget_exceeded", message="Turn 时间预算耗尽"),
                )
            else:
                recovered = await self._finish(
                    thread.thread_id,
                    turn.turn_id,
                    TurnStatus.INTERRUPTED,
                    AgentFailure(code="process_interrupted", message="上次进程中断，未自动重放"),
                )
            operation.finish(recovered.status.value, recovered.error)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._open = False
        closing = asyncio.create_task(self._close_runtime(exc_type, exc, traceback))
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await _drain(closing)
            raise

    async def _close_runtime(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        tasks = []
        for _, token, task in tuple(self._active.values()):
            token.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 答复审批不占用模型运行槽；关闭仍须等待持有 Thread 锁的复核/决定提交。
        for lock in tuple(self._locks.values()):
            async with lock:
                pass
        if self._owner is not None:
            await self._owner.__aexit__(exc_type, exc, traceback)
            self._owner = None

    def _ensure_open(self) -> None:
        if not self._open:
            raise KernelError("runtime_closed", "请在 async with AgentRuntime 中执行")

    def _lock(self, thread_id: UUID) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

    async def create_thread(self, workspace: str) -> Thread:
        self._ensure_open()
        return await self.store.append(
            new_id(),
            [EventDraft(payload=ThreadCreated(workspace=workspace))],
            expected_sequence=0,
        )

    async def _commit(
        self, thread_id: UUID, turn_id: UUID, payloads: Sequence[EventPayload]
    ) -> Thread:
        async with self._lock(thread_id):
            thread = await self.store.get_thread(thread_id)
            return await (self._batch_diffs or self.store).append(
                thread_id,
                [EventDraft(turn_id=turn_id, payload=p) for p in payloads],
                expected_sequence=thread.sequence,
            )

    async def _state(self, thread_id: UUID, turn_id: UUID, status: TurnStatus) -> Thread:
        return await self._commit(thread_id, turn_id, [TurnStateChanged(status=status)])

    async def _accept(
        self,
        thread_id: UUID,
        turn_id: UUID,
        prompt: str,
        request_id: str,
        budget: Budget,
        trace_context: TraceContext | None,
    ) -> tuple[Turn, bool]:
        content = TextContent(kind="user_message", text=prompt)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"prompt": prompt, "budget": budget.model_dump()},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        start = TurnStarted(
            request_id=request_id,
            request_fingerprint=fingerprint,
            budget=budget,
            trace_context=trace_context,
        )
        async with self._lock(thread_id):
            thread = await self.store.get_thread(thread_id)
            existing = next((t for t in thread.turns if t.request_id == request_id), None)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise KernelError("request_conflict", "request_id 已绑定不同输入或预算")
                return existing, False
            item_id = new_id()
            payloads: list[EventPayload] = [
                start,
                ItemStarted(item_id=item_id, content=content),
                ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
            ]
            thread = await self.store.append(
                thread_id,
                [EventDraft(turn_id=turn_id, payload=p) for p in payloads],
                expected_sequence=thread.sequence,
            )
            return get_turn(thread, turn_id), True

    async def run_turn(
        self,
        thread_id: UUID,
        prompt: str,
        *,
        request_id: str,
        budget: Budget | None = None,
        trace_context: TraceContext | None = None,
    ) -> Turn:
        self._ensure_open()
        limits = budget or Budget()
        turn_id = new_id()
        token = CancelToken()
        task = asyncio.current_task()
        assert task is not None
        self._active[turn_id] = (thread_id, token, task)
        try:
            with self._telemetry.operation(
                "turn",
                thread_id=thread_id,
                turn_id=turn_id,
                trace_context=trace_context,
            ) as operation:
                turn, accepted = await self._accept(
                    thread_id,
                    turn_id,
                    prompt,
                    request_id,
                    limits,
                    self._telemetry.trace_context() or trace_context,
                )
                operation.bind_turn(turn.turn_id)
                result = await self._continue(thread_id, turn_id, token) if accepted else turn
                operation.finish(result.status.value, result.error)
                return result
        except asyncio.CancelledError:
            # 接受事务的 commit 可能已经成功；取消后重新读取持久事实。
            await self._cancel_task(thread_id, turn_id)
            raise
        finally:
            self._active.pop(turn_id, None)

    async def resume_turn(self, thread_id: UUID, turn_id: UUID) -> Turn:
        self._ensure_open()
        token = CancelToken()
        task = asyncio.current_task()
        assert task is not None
        async with self._lock(thread_id):
            turn = get_turn(await self.store.get_thread(thread_id), turn_id)
            if turn.status in TERMINAL_TURNS:
                return turn
            if turn.status != TurnStatus.WAITING_APPROVAL:
                raise KernelError("turn_not_resumable", "仅可从持久审批边界继续")
            if turn_id in self._active:
                raise KernelError("turn_busy", "Turn 已在执行")
            self._active[turn_id] = (thread_id, token, task)
        try:
            with self._telemetry.operation(
                "turn",
                thread_id=thread_id,
                turn_id=turn_id,
                trace_context=turn.trace_context,
            ) as operation:
                result = await self._continue(thread_id, turn_id, token)
                operation.finish(result.status.value, result.error)
                return result
        finally:
            self._active.pop(turn_id, None)

    async def _cancel_task(self, thread_id: UUID, turn_id: UUID) -> None:
        settling = asyncio.create_task(self._settle_cancel_task(thread_id, turn_id))
        try:
            await asyncio.shield(settling)
        except asyncio.CancelledError:
            await _drain(settling)
            raise

    async def _settle_cancel_task(self, thread_id: UUID, turn_id: UUID) -> None:
        thread = await self.store.get_thread(thread_id)
        if thread.active_turn_id == turn_id:
            await self._record_cancel(thread_id, turn_id)
            await self._finish(
                thread_id,
                turn_id,
                TurnStatus.CANCELLED,
                AgentFailure(code="cancelled", message="调用方任务取消"),
            )

    async def _continue(self, thread_id: UUID, turn_id: UUID, token: CancelToken) -> Turn:
        try:
            turn = get_turn(await self.store.get_thread(thread_id), turn_id)
            remaining = remaining_seconds(turn)
            if remaining <= 0:
                raise TimeoutError
            if turn.status == TurnStatus.ACCEPTED:
                self._fault("runtime.after_turn_started")
            async with asyncio.timeout(remaining):
                result = await self._drive(thread_id, turn_id, token)
                token.checkpoint()
                return result
        except TurnCancelled:
            await self._record_cancel(thread_id, turn_id)
            return await self._finish(
                thread_id,
                turn_id,
                TurnStatus.CANCELLED,
                AgentFailure(code="cancelled", message="用户取消了当前 Turn"),
            )
        except asyncio.CancelledError:
            await self._cancel_task(thread_id, turn_id)
            raise
        except TimeoutError:
            return await self._finish(
                thread_id,
                turn_id,
                TurnStatus.FAILED,
                AgentFailure(code="time_budget_exceeded", message="Turn 时间预算耗尽"),
            )
        except Exception as exc:
            if token.cancelled:
                await self._record_cancel(thread_id, turn_id)
                return await self._finish(
                    thread_id,
                    turn_id,
                    TurnStatus.CANCELLED,
                    AgentFailure(code="cancelled", message="用户取消了当前 Turn"),
                )
            failure = (
                exc.to_failure()
                if isinstance(exc, KernelError)
                else AgentFailure(
                    code="runtime_error", message="Runtime 执行失败；原始异常未持久化"
                )
            )
            return await self._finish(thread_id, turn_id, TurnStatus.FAILED, failure)

    async def _record_cancel(self, thread_id: UUID, turn_id: UUID) -> Turn:
        async with self._lock(thread_id):
            thread = await self.store.get_thread(thread_id)
            turn = get_turn(thread, turn_id)
            if turn.status in TERMINAL_TURNS or turn.status == TurnStatus.CANCELLING:
                return turn
            if turn.status == TurnStatus.WAITING_ACTION:
                raise KernelError(
                    "process_action_not_enabled", "Process Action等待取消将在运行时接线后开放"
                )
            updated = await self.store.append(
                thread_id,
                [
                    EventDraft(
                        turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.CANCELLING)
                    )
                ],
                expected_sequence=thread.sequence,
            )
            return get_turn(updated, turn_id)

    async def cancel(self, thread_id: UUID, turn_id: UUID) -> Turn:
        self._ensure_open()
        turn = get_turn(await self.store.get_thread(thread_id), turn_id)
        with self._telemetry.operation(
            "cancel",
            thread_id=thread_id,
            turn_id=turn_id,
            trace_context=turn.trace_context,
        ) as operation:
            result = await self._cancel(thread_id, turn_id)
            operation.finish(result.status.value, result.error)
            return result

    async def _cancel(self, thread_id: UUID, turn_id: UUID) -> Turn:
        self._ensure_open()
        turn = await self._record_cancel(thread_id, turn_id)
        active = self._active.get(turn_id)
        if active is not None:
            active[1].cancel()
        elif turn.status not in TERMINAL_TURNS:
            return await self._finish(
                thread_id,
                turn_id,
                TurnStatus.CANCELLED,
                AgentFailure(code="cancelled", message="用户取消了暂停的 Turn"),
            )
        return turn

    async def reply_approval(
        self,
        thread_id: UUID,
        turn_id: UUID,
        approval_id: UUID,
        *,
        fingerprint: str,
        decision: ApprovalDecision,
    ) -> Turn:
        self._ensure_open()
        turn = get_turn(await self.store.get_thread(thread_id), turn_id)
        with self._telemetry.operation(
            "approval",
            thread_id=thread_id,
            turn_id=turn_id,
            trace_context=turn.trace_context,
        ) as operation:
            result = await self._reply_approval(
                thread_id,
                turn_id,
                approval_id,
                fingerprint=fingerprint,
                decision=decision,
            )
            operation.finish(decision.outcome.value)
            return result

    async def _reply_approval(
        self,
        thread_id: UUID,
        turn_id: UUID,
        approval_id: UUID,
        *,
        fingerprint: str,
        decision: ApprovalDecision,
    ) -> Turn:
        self._ensure_open()
        decision = ApprovalDecision.model_validate_json(decision.model_dump_json())
        async with self._lock(thread_id):
            thread = await self.store.get_thread(thread_id)
            turn = get_turn(thread, turn_id)
            item = next(
                (
                    i
                    for i in turn.items
                    if isinstance(i.content, ApprovalContent)
                    and i.content.approval_id == approval_id
                ),
                None,
            )
            if item is None:
                raise KernelError("approval_not_found", "审批请求不存在")
            assert isinstance(item.content, ApprovalContent)
            content = item.content
            if fingerprint != content.request_fingerprint:
                raise KernelError("approval_mismatch", "审批指纹不匹配")
            if isinstance(content, ProcessApprovalRequestContent):
                raise KernelError(
                    "process_action_not_enabled", "Process审批必须由Action事实投影，普通答复未开放"
                )
            if content.decision is not None:
                recorded = content.decision
                if (recorded.outcome, recorded.actor, recorded.reason) != (
                    decision.outcome,
                    decision.actor,
                    decision.reason,
                ):
                    raise KernelError("approval_conflict", "审批已绑定其他决定")
                return turn
            if turn.status != TurnStatus.WAITING_APPROVAL or item.status != ItemStatus.STARTED:
                raise KernelError("approval_closed", "审批请求已关闭")
            if remaining_seconds(turn) <= 0:
                raise KernelError("approval_expired", "审批已超过 Turn 时间预算")
            call = pending_calls(turn)[0]
            if call.call_id != content.call_id or not approval_matches(thread, turn, call, content):
                raise KernelError("approval_mismatch", "审批与当前调用不匹配")
            self._validate_tool_contract(call)
            if isinstance(content, PatchBatchApprovalRequestContent):
                if self._patch_batches is None:
                    raise KernelError("patch_batch_not_enabled", "未配置原整组 Patch 端口")
                try:
                    async with asyncio.timeout(remaining_seconds(turn)):
                        await self._patch_batches.review(
                            call,
                            inspection_scope(thread, turn, call),
                            content.plan,
                            CancelToken(),
                            verify_source=decision.outcome == ApprovalOutcome.APPROVED,
                        )
                except TimeoutError:
                    raise KernelError(
                        "approval_expired", "整组审批复核超过原 Turn 截止时间"
                    ) from None
                if remaining_seconds(turn) <= 0:
                    raise KernelError("approval_expired", "整组审批复核后原 Turn 预算已耗尽")
            if isinstance(content, PatchApprovalRequestContent):
                if self._patches is None:
                    raise KernelError("patch_not_enabled", "未配置原 Patch 专用入口")
                await self._patches.review(
                    call,
                    inspection_scope(thread, turn, call),
                    content.plan,
                    CancelToken(),
                    verify_source=decision.outcome == ApprovalOutcome.APPROVED,
                )
                if remaining_seconds(turn) <= 0:
                    raise KernelError("approval_expired", "审批复核后 Turn 时间预算已耗尽")
            record = ApprovalRecord(
                **decision.model_dump(),
                request_fingerprint=fingerprint,
                decided_at=utc_now(),
            )
            self._fault("runtime.before_approval_decision")
            updated = await self.store.append(
                thread_id,
                [
                    EventDraft(
                        turn_id=turn_id,
                        occurred_at=record.decided_at,
                        payload=ItemFinished(
                            item_id=item.item_id,
                            status=ItemStatus.COMPLETED,
                            content=content.model_copy(update={"decision": record}),
                        ),
                    )
                ],
                expected_sequence=thread.sequence,
            )
            self._fault("runtime.after_approval_decision")
            return get_turn(updated, turn_id)

    def _validate_tool_contract(self, call: ToolCallContent) -> None:
        definition = self._definitions.get(call.tool)
        if (
            definition is None
            or definition.version != call.tool_version
            or definition.effect_class != call.effect_class
            or definition.requires_approval != call.requires_approval
            or tool_fingerprint(definition) != call.tool_fingerprint
        ):
            raise KernelError("tool_contract_changed", "工具契约已变化，旧调用不可继续")

    async def _drive(self, thread_id: UUID, turn_id: UUID, token: CancelToken) -> Turn:
        while True:
            token.checkpoint()
            turn = get_turn(await self.store.get_thread(thread_id), turn_id)
            if turn.status != TurnStatus.WAITING_APPROVAL:
                thread = await self._state(thread_id, turn_id, TurnStatus.PREPARING_CONTEXT)
                turn = get_turn(thread, turn_id)
                if (
                    turn.model_steps >= turn.budget.max_steps
                    or turn.usage.total_tokens >= turn.budget.max_tokens
                ):
                    raise KernelError("budget_exceeded", "模型步骤或 Token 预算耗尽")
                thread = await self._state(thread_id, turn_id, TurnStatus.CALLING_MODEL)
                turn = get_turn(thread, turn_id)
                request = ModelRequest(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    step=turn.model_steps,
                    history=tuple(
                        item
                        for previous in thread.turns
                        for item in previous.items
                        if item.status == ItemStatus.COMPLETED
                        and isinstance(
                            item.content, TextContent | ToolCallContent | ToolResultContent
                        )
                    ),
                    tools=tuple(
                        d
                        for d in self._definitions.values()
                        if d.effect_class == EffectClass.READ_ONLY
                        or (self._patches is not None and d.name == "apply_patch")
                        or (self._patch_batches is not None and d.name == "apply_patch_batch")
                    ),
                    budget=turn.budget,
                    remaining_tokens=turn.budget.max_tokens - turn.usage.total_tokens,
                )
                await self._sample(request, token)
                turn = get_turn(await self.store.get_thread(thread_id), turn_id)
                calls = pending_calls(turn)
                if turn.usage.total_tokens > turn.budget.max_tokens:
                    raise KernelError("budget_exceeded", "Provider 报告的 Token 用量超过预算")
                if not calls:
                    token.checkpoint()
                    await self._state(thread_id, turn_id, TurnStatus.FINALIZING)
                    return await self._finish(thread_id, turn_id, TurnStatus.COMPLETED, None)
                if turn.usage.total_tokens >= turn.budget.max_tokens:
                    raise KernelError("budget_exceeded", "Token 预算耗尽，停止调度工具")
                token.checkpoint()
                await self._state(thread_id, turn_id, TurnStatus.EXECUTING_TOOLS)
            waiting = await self._execute_calls(thread_id, turn_id, token)
            if waiting is not None:
                return waiting

    async def _execute_calls(
        self,
        thread_id: UUID,
        turn_id: UUID,
        token: CancelToken,
    ) -> Turn | None:
        thread = await self.store.get_thread(thread_id)
        turn = get_turn(thread, turn_id)
        for call in pending_calls(turn):
            token.checkpoint()
            rejected = False
            early_result: ToolResultContent | None = None
            existing = approval_for(turn, call)
            if existing is not None:
                if (
                    isinstance(existing.content, PatchBatchApprovalRequestContent)
                    and self._patch_batches is None
                ):
                    raise KernelError("patch_batch_not_enabled", "持久整组审批缺少原专用端口")
                if (
                    isinstance(existing.content, PatchApprovalRequestContent)
                    and self._patches is None
                ):
                    raise KernelError("patch_not_enabled", "持久单文件审批缺少原专用端口")
            is_patch = self._patches is not None and call.tool == "apply_patch"
            is_batch = self._patch_batches is not None and call.tool == "apply_patch_batch"
            if (
                is_patch
                or is_batch
                or (call.requires_approval and call.effect_class == EffectClass.READ_ONLY)
            ):
                self._validate_tool_contract(call)
                item = approval_for(turn, call)
                if item is None:
                    content: ApprovalContent | None = None
                    if is_patch or is_batch:
                        try:
                            scope = ToolExecutionScope.for_pending_call(thread, turn_id, call)
                            if is_batch:
                                assert self._patch_batches is not None
                                batch_plan = await self._patch_batches.prepare(call, scope, token)
                                content = PatchBatchApprovalRequestContent(
                                    approval_id=new_id(),
                                    call_id=call.call_id,
                                    plan=batch_plan,
                                    request_fingerprint=batch_plan.approval_fingerprint,
                                )
                            else:
                                assert self._patches is not None
                                plan = await self._patches.prepare(call, scope, token)
                                content = PatchApprovalRequestContent(
                                    approval_id=new_id(),
                                    call_id=call.call_id,
                                    plan=plan,
                                    request_fingerprint=plan.approval_fingerprint,
                                )
                        except KernelError as error:
                            if error.code not in {
                                "tool_invalid_arguments",
                                "patch_source_changed",
                                "patch_context_not_found",
                                "patch_ambiguous_context",
                                "patch_overlapping_edits",
                                "patch_no_change",
                                "patch_limit_exceeded",
                                "patch_path_denied",
                                "patch_not_found",
                            }:
                                raise
                            early_result = ToolResultContent(
                                call_id=call.call_id, outcome="failed", error=error.to_failure()
                            )
                        else:
                            self._fault(
                                "runtime.after_patch_batch_plan"
                                if is_batch
                                else "runtime.after_patch_plan"
                            )
                    else:
                        content = ApprovalRequestContent(
                            approval_id=new_id(),
                            call_id=call.call_id,
                            request_fingerprint=request_fingerprint(thread, turn, call),
                        )
                    if content is not None:
                        self._fault("runtime.before_approval_request")
                        thread = await self._commit(
                            thread_id,
                            turn_id,
                            [
                                ItemStarted(item_id=new_id(), content=content),
                                TurnStateChanged(status=TurnStatus.WAITING_APPROVAL),
                            ],
                        )
                        self._fault("runtime.after_approval_request")
                        return get_turn(thread, turn_id)
                elif item.status == ItemStatus.STARTED:
                    return turn
                else:
                    assert isinstance(item.content, ApprovalContent)
                    decision = item.content.decision
                    if decision is None or not approval_matches(thread, turn, call, item.content):
                        raise KernelError("approval_mismatch", "持久审批与当前调用不匹配")
                    rejected = (
                        not (is_patch or is_batch) and decision.outcome == ApprovalOutcome.REJECTED
                    )
                    # 持久离开等待状态即消费恢复边界；之后崩溃只能核对，不能再次执行。
                    thread = await self._state(thread_id, turn_id, TurnStatus.EXECUTING_TOOLS)
                    turn = get_turn(thread, turn_id)
                    self._fault("runtime.after_approval_consumed")
            token.checkpoint()
            result = early_result or (
                ToolResultContent(
                    call_id=call.call_id,
                    outcome="failed",
                    error=AgentFailure(code="approval_rejected", message="用户拒绝了工具调用"),
                )
                if rejected
                else await self._execute(
                    thread_id, turn_id, call, token, turn.budget.max_output_chars
                )
            )
            token.checkpoint()
            if isinstance(result, ArtifactToolResult):
                if self._artifacts is None:
                    raise KernelError("artifact_not_enabled", "未配置 Artifact 发布器，正文未保存")
                async with self._lock(thread_id):
                    current = await self.store.get_thread(thread_id)
                    thread = await self._artifacts.publish(
                        thread_id,
                        turn_id,
                        call,
                        result,
                        expected_sequence=current.sequence,
                        max_output_chars=turn.budget.max_output_chars,
                    )
                settled_result = result.result
            else:
                if rejected or early_result is not None:
                    result = self._validate_result(result, call, turn.budget.max_output_chars)
                item_id = new_id()
                thread = await self._commit(
                    thread_id,
                    turn_id,
                    [
                        ItemStarted(item_id=item_id, content=result),
                        ItemFinished(item_id=item_id, content=result, status=ItemStatus.COMPLETED),
                    ],
                )
                settled_result = result
                self._fault("runtime.after_tool_result")
            turn = get_turn(thread, turn_id)
            if settled_result.outcome == "unknown":
                raise KernelError("uncertain_effect", "工具结果未知，禁止继续模型循环")
            if (
                settled_result.patch_batch is not None
                and settled_result.patch_batch.execution is not None
            ):
                reason = settled_result.patch_batch.execution.run.stop_reason
                if reason == "cancelled":
                    raise TurnCancelled
                if reason != "completed":
                    raise KernelError(
                        "patch_timeout" if reason == "timeout" else "patch_batch_failed",
                        "整组运行未正常完成；已归因效果仍保留",
                    )
        return None

    async def _execute(
        self,
        thread_id: UUID,
        turn_id: UUID,
        call: ToolCallContent,
        token: CancelToken,
        max_output_chars: int,
    ) -> ToolResultContent | ArtifactToolResult:
        with self._telemetry.operation(
            "tool",
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call.call_id,
        ) as operation:
            result = await self._execute_tool(thread_id, turn_id, call, token)
            if isinstance(result, ArtifactToolResult):
                if self._artifacts is None:
                    raise KernelError("artifact_not_enabled", "未配置 Artifact 发布器，正文未保存")
                if result.publisher is not self._artifacts:
                    raise KernelError("artifact_store_mismatch", "Artifact 载荷没有匹配的发布器")
                checked = self._validate_result(result.result, call, max_output_chars)
                operation.finish(checked.outcome, checked.error)
                return replace(result, result=checked)
            result = self._validate_result(result, call, max_output_chars)
            operation.finish(result.outcome, result.error)
            return result

    @staticmethod
    def _validate_result(
        result: ToolResultContent, call: ToolCallContent, max_chars: int
    ) -> ToolResultContent:
        content = ToolResultContent.model_validate_json(result.model_dump_json())
        if content.call_id != call.call_id:
            raise KernelError("tool_result_mismatch", "工具结果与调用 ID 不匹配")
        # Patch 证据是固定有界的 Session 元数据，不挤占模型公开结果预算。
        if len(content.model_dump_json(exclude={"patch", "patch_batch"})) > max_chars:
            raise KernelError("tool_output_too_large", "工具输出超过当前 Kernel 上限")
        return content

    async def _execute_tool(
        self, thread_id: UUID, turn_id: UUID, call: ToolCallContent, token: CancelToken
    ) -> ToolResultContent | ArtifactToolResult:
        definition = self._definitions.get(call.tool)
        if definition is None:
            return ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                error=AgentFailure(code="unknown_tool", message="工具未注册"),
            )
        if self._patch_batches is not None and call.tool == "apply_patch_batch":
            self._validate_tool_contract(call)
            thread = await self.store.get_thread(thread_id)
            turn = get_turn(thread, turn_id)
            batch_approval = batch_patching.execution_approval(thread, turn, call)
            assert batch_approval.decision is not None
            batch_scope = ToolExecutionScope.for_pending_call(thread, turn_id, call)
            token.checkpoint()
            self._fault("runtime.before_tool")
            batch_result = await self._patch_batches.execute(
                call, batch_scope, batch_approval.plan, batch_approval.decision, token
            )
            self._fault("runtime.after_tool")
            return batch_patching.result_content(batch_result, thread, turn, call, "execution")
        if self._patches is not None and call.tool == "apply_patch":
            self._validate_tool_contract(call)
            thread = await self.store.get_thread(thread_id)
            turn = get_turn(thread, turn_id)
            approval = execution_approval(thread, turn, call)
            assert approval.decision is not None
            patch_scope = ToolExecutionScope.for_pending_call(thread, turn_id, call)
            token.checkpoint()
            self._fault("runtime.before_tool")
            settled = await self._patches.execute(
                call, patch_scope, approval.plan, approval.decision, token
            )
            self._fault("runtime.after_tool")
            return result_content(settled, call, "execution")
        if definition.effect_class != EffectClass.READ_ONLY:
            return ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                error=AgentFailure(code="tool_not_enabled", message="当前 Kernel 切片未开放写工具"),
            )
        self._validate_tool_contract(call)
        scope = (
            ToolExecutionScope.for_pending_call(
                await self.store.get_thread(thread_id), turn_id, call
            )
            if self._scoped_tools is not None
            else None
        )
        token.checkpoint()
        self._fault("runtime.before_tool")
        if self._scoped_tools is not None:
            assert scope is not None
            result = await token.run(
                self._scoped_tools.execute_scoped(call.model_copy(deep=True), scope, token)
            )
        else:
            result = await token.run(self._legacy_tools.execute(call.model_copy(deep=True), token))
            if isinstance(result, ArtifactToolResult):
                raise KernelError("artifact_scope_required", "Artifact 只能由 Scoped 入口返回")
        self._fault("runtime.after_tool")
        return result

    async def _sample(self, request: ModelRequest, token: CancelToken) -> None:
        with self._telemetry.operation(
            "model",
            thread_id=request.thread_id,
            turn_id=request.turn_id,
            step=request.step,
        ):
            await self._sample_events(request, token)

    async def _sample_events(self, request: ModelRequest, token: CancelToken) -> None:
        started = False
        completed: ResponseCompleted | None = None
        text_items: dict[str, tuple[UUID, str, bool]] = {}
        call_ids: set[str] = set()
        characters = 0
        stream_sequence = 0
        event_count = 0
        attempt_mode = False
        open_attempt: UUID | None = None
        attempt_response_id: str | None = None
        response_id: str | None = None
        accounted = get_turn(await self.store.get_thread(request.thread_id), request.turn_id).usage

        def record_usage(thread: Thread) -> None:
            nonlocal accounted
            current = get_turn(thread, request.turn_id).usage
            self._telemetry.usage(
                Usage(
                    input_tokens=current.input_tokens - accounted.input_tokens,
                    output_tokens=current.output_tokens - accounted.output_tokens,
                )
            )
            accounted = current

        async with aclosing(self.provider.stream(request, token)) as stream:
            while True:
                token.checkpoint()
                try:
                    event = await token.run(anext(stream))
                except StopAsyncIteration:
                    break
                if completed is not None:
                    raise KernelError("invalid_provider_output", "Provider 在终态之后继续输出")
                event_count += 1
                if event_count > 10000:
                    raise KernelError("provider_event_limit", "模型步骤事件数超过上限")
                if isinstance(
                    event, ModelAttemptStarted | ModelUsageObserved | ModelAttemptFinished
                ):
                    if isinstance(event, ModelAttemptStarted):
                        if started:
                            raise KernelError(
                                "invalid_provider_output", "响应开始后不得重试模型请求"
                            )
                        if accounted.total_tokens >= request.budget.max_tokens:
                            raise KernelError("budget_exceeded", "模型尝试的已知 Token 预算耗尽")
                    if isinstance(event, ModelUsageObserved) and (
                        response_id is not None
                        and event.response_id is not None
                        and event.response_id != response_id
                    ):
                        raise KernelError("invalid_provider_output", "用量响应身份与当前响应不一致")
                    try:
                        snapshot = await self._commit(request.thread_id, request.turn_id, [event])
                    except KernelError as error:
                        if error.code == "invalid_event":
                            raise KernelError(
                                "invalid_provider_output", "模型尝试事实不符合契约"
                            ) from None
                        raise
                    if isinstance(event, ModelAttemptStarted):
                        attempt_mode = True
                        open_attempt = event.attempt_id
                        attempt_response_id = None
                        # 提交后才继续消费：Provider 必须在下次 anext 才发起 HTTP。
                        self._fault("runtime.after_model_attempt_started")
                    elif isinstance(event, ModelUsageObserved):
                        attempt_response_id = event.response_id or attempt_response_id
                        record_usage(snapshot)
                        self._fault("runtime.after_model_usage_observed")
                    else:
                        open_attempt = None
                        self._fault("runtime.after_model_attempt_finished")
                    continue
                if isinstance(event, ResponseFailed):
                    raise KernelError(
                        "provider_" + event.code,
                        "Provider 返回结构化失败",
                        retryable=event.retryable,
                    )
                if isinstance(event, ResponseStarted):
                    if started or (attempt_mode and open_attempt is None):
                        raise KernelError("invalid_provider_output", "Provider 重复开始响应")
                    if attempt_response_id is not None and event.response_id != attempt_response_id:
                        raise KernelError("invalid_provider_output", "当前响应与尝试身份不一致")
                    response_id = event.response_id
                    started = True
                    continue
                if not started:
                    raise KernelError("invalid_provider_output", "Provider 尚未开始响应")
                if isinstance(event, TextStarted):
                    if len(text_items) >= 128:
                        raise KernelError("provider_item_limit", "模型步骤文本块数量超过上限")
                    if event.content_id in text_items:
                        raise KernelError("invalid_provider_output", "文本块 ID 重复")
                    item_id = new_id()
                    text_items[event.content_id] = (item_id, "", False)
                    await self._commit(
                        request.thread_id,
                        request.turn_id,
                        [
                            ItemStarted(
                                item_id=item_id, content=TextContent(kind="assistant_message")
                            )
                        ],
                    )
                elif isinstance(event, TextDelta | TextCompleted):
                    part = text_items.get(event.content_id)
                    if part is None or part[2]:
                        raise KernelError("invalid_provider_output", "文本块未开始或已结束")
                    item_id, buffer, _ = part
                    if isinstance(event, TextDelta):
                        characters += len(event.delta)
                        if characters > request.budget.max_output_chars:
                            raise KernelError("model_output_too_large", "模型输出超过上限")
                        buffer += event.delta
                        text_items[event.content_id] = (item_id, buffer, False)
                        stream_sequence += 1
                        self._on_delta(
                            ItemDelta(
                                thread_id=request.thread_id,
                                turn_id=request.turn_id,
                                item_id=item_id,
                                model_step=request.step,
                                stream_sequence=stream_sequence,
                                delta=event.delta,
                            )
                        )
                    else:
                        if buffer and buffer != event.text:
                            raise KernelError("invalid_provider_output", "文本终值与增量不一致")
                        if not buffer:
                            characters += len(event.text)
                        if characters > request.budget.max_output_chars:
                            raise KernelError("model_output_too_large", "模型输出超过上限")
                        text_items[event.content_id] = (item_id, event.text, True)
                        await self._commit(
                            request.thread_id,
                            request.turn_id,
                            [
                                ItemFinished(
                                    item_id=item_id,
                                    status=ItemStatus.COMPLETED,
                                    content=TextContent(kind="assistant_message", text=event.text),
                                )
                            ],
                        )
                elif isinstance(event, ToolCallCompleted):
                    if event.call_id in call_ids:
                        raise KernelError("invalid_provider_output", "Provider Tool Call ID 重复")
                    call_ids.add(event.call_id)
                    if len(call_ids) > request.budget.max_tool_calls_per_step:
                        raise KernelError("tool_call_limit", "单步骤 Tool Call 数量超过上限")
                    characters += len(event.model_dump_json())
                    if characters > request.budget.max_output_chars:
                        raise KernelError("model_output_too_large", "工具参数超过模型输出上限")
                    definition = self._definitions.get(event.tool)
                    call = ToolCallContent(
                        call_id=new_id(),
                        provider_call_id=event.call_id,
                        tool=event.tool,
                        tool_version=definition.version if definition else "unregistered",
                        effect_class=definition.effect_class
                        if definition
                        else EffectClass.READ_ONLY,
                        arguments=event.arguments,
                        requires_approval=definition.requires_approval if definition else False,
                        tool_fingerprint=tool_fingerprint(definition) if definition else None,
                    )
                    item_id = new_id()
                    await self._commit(
                        request.thread_id,
                        request.turn_id,
                        [
                            ItemStarted(item_id=item_id, content=call),
                            ItemFinished(
                                item_id=item_id, status=ItemStatus.COMPLETED, content=call
                            ),
                        ],
                    )
                    self._fault("runtime.after_tool_call")
                elif isinstance(event, ResponseCompleted):
                    if any(not part[2] for part in text_items.values()):
                        raise KernelError("invalid_provider_output", "响应结束时文本块尚未完成")
                    try:
                        snapshot = await self._commit(
                            request.thread_id,
                            request.turn_id,
                            [UsageRecorded(step=request.step, usage=event.usage)],
                        )
                    except KernelError as error:
                        if attempt_mode and error.code == "invalid_event":
                            raise KernelError(
                                "invalid_provider_output", "响应用量与尝试事实不一致"
                            ) from None
                        raise
                    if not attempt_mode:
                        record_usage(snapshot)
                    if event.finish_reason not in {"completed", "tool_calls"}:
                        raise KernelError("provider_" + event.finish_reason, "模型未正常完成")
                    if bool(call_ids) != (event.finish_reason == "tool_calls"):
                        raise KernelError("invalid_provider_output", "停止原因与 Tool Call 不一致")
                    if not call_ids and not any(part[1] for part in text_items.values()):
                        raise KernelError("invalid_provider_output", "模型响应没有语义内容")
                    completed = event
                else:
                    raise KernelError("invalid_provider_output", "不支持的 Provider 事件")
        if not started or completed is None:
            raise KernelError("provider_stream_incomplete", "Provider 流缺少完整终态")

    async def _recover_patch(
        self, thread: Thread, turn: Turn, call: ToolCallContent
    ) -> ToolResultContent:
        try:
            if self._patches is None:
                raise KernelError("patch_not_enabled", "原 Patch 核对入口不可用")
            self._validate_tool_contract(call)
            item = approval_for(turn, call)
            content = item.content if item is not None else None
            if content is not None and not isinstance(content, PatchApprovalRequestContent):
                raise KernelError("approval_mismatch", "写调用不匹配写审批")
            if content is not None and not approval_matches(thread, turn, call, content):
                raise KernelError("approval_mismatch", "写调用与持久计划不一致")
            settled = await self._patches.recover(
                call,
                inspection_scope(thread, turn, call),
                CancelToken(),
                plan=content.plan if content else None,
                approval=content.decision if content else None,
            )
            result = result_content(settled, call, "recovery")
            if (
                len(result.model_dump_json(exclude={"patch", "patch_batch"}))
                > turn.budget.max_output_chars
            ):
                result = result.model_copy(update={"output": None})
            return result
        except Exception as error:
            failure = (
                error.to_failure()
                if isinstance(error, KernelError)
                else AgentFailure(
                    code="patch_recovery_failed", message="Patch 核对失败；原始错误未持久化"
                )
            )
            return ToolResultContent(call_id=call.call_id, outcome="unknown", error=failure)

    async def _recover_patch_batch(
        self, thread: Thread, turn: Turn, call: ToolCallContent
    ) -> ToolResultContent:
        try:
            if self._patch_batches is None:
                raise KernelError("patch_batch_not_enabled", "原整组核对端口不可用")
            self._validate_tool_contract(call)
            item = approval_for(turn, call)
            content = item.content if item is not None else None
            if content is not None and (
                not isinstance(content, PatchBatchApprovalRequestContent)
                or not approval_matches(thread, turn, call, content)
            ):
                raise KernelError("approval_mismatch", "整组调用与持久计划不一致")
            assert content is None or isinstance(content, PatchBatchApprovalRequestContent)
            settled = await self._patch_batches.recover(
                call,
                inspection_scope(thread, turn, call),
                CancelToken(),
                plan=content.plan if content else None,
                approval=content.decision if content else None,
            )
            result = batch_patching.result_content(settled, thread, turn, call, "recovery")
            if (
                len(result.model_dump_json(exclude={"patch", "patch_batch"}))
                > turn.budget.max_output_chars
            ):
                result = result.model_copy(update={"output": None})
            return result
        except Exception as error:
            failure = (
                error.to_failure()
                if isinstance(error, KernelError)
                else AgentFailure(
                    code="patch_batch_recovery_failed", message="整组核对失败；原始错误未持久化"
                )
            )
            return ToolResultContent(call_id=call.call_id, outcome="unknown", error=failure)

    async def _finish(
        self,
        thread_id: UUID,
        turn_id: UUID,
        status: TurnStatus,
        error: AgentFailure | None,
    ) -> Turn:
        async with self._lock(thread_id):
            thread = await self.store.get_thread(thread_id)
            turn = get_turn(thread, turn_id)
            if turn.status in TERMINAL_TURNS:
                return turn
            recorded_results = {
                i.content.call_id for i in turn.items if isinstance(i.content, ToolResultContent)
            }
            recovered = {}
            for call in pending_calls(turn):
                if (
                    call.call_id not in recorded_results
                    and call.tool == "apply_patch"
                    and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
                ):
                    recovered[call.call_id] = await self._recover_patch(thread, turn, call)
                elif (
                    call.call_id not in recorded_results
                    and call.tool == "apply_patch_batch"
                    and call.effect_class == EffectClass.NON_IDEMPOTENT_WRITE
                ):
                    recovered[call.call_id] = await self._recover_patch_batch(thread, turn, call)
            if turn.status == TurnStatus.CANCELLING and status != TurnStatus.INTERRUPTED:
                status = TurnStatus.CANCELLED
                error = AgentFailure(code="cancelled", message="取消已生效")
            if any(
                isinstance(i.content, ToolResultContent) and i.content.outcome == "unknown"
                for i in turn.items
            ) or any(
                c.effect_class != EffectClass.READ_ONLY
                and (c.call_id not in recovered or recovered[c.call_id].outcome == "unknown")
                for c in pending_calls(turn)
            ):
                status = TurnStatus.INTERRUPTED
                error = AgentFailure(code="uncertain_effect", message="存在未知效果，禁止自动重放")
            payloads: list[EventPayload] = []
            for attempt in turn.model_attempts:
                if attempt.status == "running":
                    payloads.append(
                        ModelAttemptFinished(
                            attempt_id=attempt.attempt_id,
                            outcome="cancelled"
                            if status == TurnStatus.CANCELLED
                            else "interrupted"
                            if status == TurnStatus.INTERRUPTED
                            else "failed",
                            error=error
                            or AgentFailure(
                                code="provider_stream_incomplete", message="模型尝试未结束"
                            ),
                        )
                    )
            for item in turn.items:
                if item.status == ItemStatus.STARTED:
                    payloads.append(
                        ItemFinished(
                            item_id=item.item_id,
                            status=ItemStatus.CANCELLED
                            if status == TurnStatus.CANCELLED
                            else ItemStatus.FAILED,
                            content=item.content,
                            error=error,
                        )
                    )
            recorded_results = {
                i.content.call_id for i in turn.items if isinstance(i.content, ToolResultContent)
            }
            for call in pending_calls(turn):
                if call.call_id in recorded_results:
                    continue
                # Patch 只允许上面的专用核对；不调用通用 ToolRuntime 或重放写入。
                result = recovered.get(call.call_id) or ToolResultContent(
                    call_id=call.call_id,
                    outcome=(
                        "unknown"
                        if call.effect_class != EffectClass.READ_ONLY
                        else "cancelled"
                        if status == TurnStatus.CANCELLED
                        else "failed"
                    ),
                    error=error,
                )
                item_id = new_id()
                payloads.extend(
                    [
                        ItemStarted(item_id=item_id, content=result),
                        ItemFinished(item_id=item_id, status=ItemStatus.COMPLETED, content=result),
                    ]
                )
            if error is not None:
                error_content = ErrorContent(failure=error)
                error_item_id = new_id()
                payloads.extend(
                    [
                        ItemStarted(item_id=error_item_id, content=error_content),
                        ItemFinished(
                            item_id=error_item_id,
                            content=error_content,
                            status=ItemStatus.COMPLETED,
                        ),
                    ]
                )
            payloads.append(TurnStateChanged(status=status, error=error))
            self._fault("runtime.before_terminal")
            thread = await (self._batch_diffs or self.store).append(
                thread_id,
                [EventDraft(turn_id=turn_id, payload=p) for p in payloads],
                expected_sequence=thread.sequence,
            )
            completed = get_turn(thread, turn_id)
            self._telemetry.finished(completed)
            return completed

    async def action_context(self, thread_id: UUID, turn_id: UUID) -> ActionContext:
        turn = get_turn(await self.store.get_thread(thread_id), turn_id)
        parts = turn.trace_context.traceparent.split("-") if turn.trace_context else []
        trace_id = parts[1] if len(parts) == 4 else None
        return ActionContext(session_id=str(thread_id), run_id=str(turn_id), trace_id=trace_id)
