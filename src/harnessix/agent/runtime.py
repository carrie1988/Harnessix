from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, aclosing
from types import TracebackType
from typing import Self
from uuid import UUID

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    TERMINAL_TURNS,
    AgentFailure,
    Budget,
    EventDraft,
    EventPayload,
    ItemDelta,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    Thread,
    ThreadCreated,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    UsageRecorded,
)
from harnessix.agent.ports import NoTools, ToolRuntime
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.domain.models import ActionContext, EffectClass, TraceContext
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
from harnessix.session.ports import SessionStore


class AgentRuntime:
    """进程内 Kernel 宿主；不承担 CLI、模型 SDK、Shell 或 Sandbox 职责。"""

    def __init__(
        self,
        store: SessionStore,
        provider: ModelProvider,
        tools: ToolRuntime | None = None,
        *,
        on_delta: Callable[[ItemDelta], None] | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.tools = tools or NoTools()
        definitions = self.tools.definitions()
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
        await self.store.initialize()
        owner = self.store.runtime_owner()
        await owner.__aenter__()
        self._owner = owner
        self._open = True
        try:
            for thread_id in await self.store.thread_ids():
                thread = await self.store.get_thread(thread_id)
                if thread.active_turn_id is not None:
                    await self._finish(
                        thread_id,
                        thread.active_turn_id,
                        TurnStatus.INTERRUPTED,
                        AgentFailure(
                            code="process_interrupted", message="上次进程中断，未自动重放"
                        ),
                    )
        except BaseException:
            self._open = False
            self._owner = None
            await owner.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._open = False
        tasks = []
        for _, token, task in tuple(self._active.values()):
            token.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
            return await self.store.append(
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
        accepted = False
        try:
            turn, accepted = await self._accept(
                thread_id, turn_id, prompt, request_id, limits, trace_context
            )
            if not accepted:
                return turn
            self._fault("runtime.after_turn_started")
            async with asyncio.timeout(limits.timeout_seconds):
                return await self._drive(thread_id, turn_id, token)
        except TurnCancelled:
            await self._record_cancel(thread_id, turn_id)
            return await self._finish(
                thread_id,
                turn_id,
                TurnStatus.CANCELLED,
                AgentFailure(code="cancelled", message="用户取消了当前 Turn"),
            )
        except asyncio.CancelledError:
            # 取消可能发生于接受事务的 commit 等待期间；重新读取事实再决定是否清理。
            thread = await self.store.get_thread(thread_id)
            if thread.active_turn_id == turn_id:
                await self._record_cancel(thread_id, turn_id)
                await self._finish(
                    thread_id,
                    turn_id,
                    TurnStatus.CANCELLED,
                    AgentFailure(code="cancelled", message="调用方任务取消"),
                )
            raise
        except TimeoutError:
            return await self._finish(
                thread_id,
                turn_id,
                TurnStatus.FAILED,
                AgentFailure(code="time_budget_exceeded", message="Turn 时间预算耗尽"),
            )
        except Exception as exc:
            if not accepted:
                raise
            if token.cancelled:
                await self._record_cancel(thread_id, turn_id)
                return await self._finish(
                    thread_id,
                    turn_id,
                    TurnStatus.CANCELLED,
                    AgentFailure(code="cancelled", message="用户取消了当前 Turn"),
                )
            failure = (
                AgentFailure(code=exc.code, message=exc.message)
                if isinstance(exc, KernelError)
                else AgentFailure(
                    code="runtime_error", message="Runtime 执行失败；原始异常未持久化"
                )
            )
            return await self._finish(thread_id, turn_id, TurnStatus.FAILED, failure)
        finally:
            self._active.pop(turn_id, None)

    async def _record_cancel(self, thread_id: UUID, turn_id: UUID) -> Turn:
        async with self._lock(thread_id):
            thread = await self.store.get_thread(thread_id)
            turn = get_turn(thread, turn_id)
            if turn.status in TERMINAL_TURNS or turn.status == TurnStatus.CANCELLING:
                return turn
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
        turn = await self._record_cancel(thread_id, turn_id)
        active = self._active.get(turn_id)
        if active is not None:
            active[1].cancel()
        return turn

    async def _drive(self, thread_id: UUID, turn_id: UUID, token: CancelToken) -> Turn:
        while True:
            token.checkpoint()
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
                ),
                tools=tuple(
                    d
                    for d in self._definitions.values()
                    if d.effect_class == EffectClass.READ_ONLY and not d.requires_approval
                ),
                budget=turn.budget,
            )
            await self._sample(request, token)
            turn = get_turn(await self.store.get_thread(thread_id), turn_id)
            calls = pending_calls(turn)
            if turn.usage.total_tokens > turn.budget.max_tokens:
                raise KernelError("budget_exceeded", "Provider 报告的 Token 用量超过预算")
            if calls:
                if turn.usage.total_tokens >= turn.budget.max_tokens:
                    raise KernelError("budget_exceeded", "Token 预算耗尽，停止调度工具")
                token.checkpoint()
                await self._state(thread_id, turn_id, TurnStatus.EXECUTING_TOOLS)
                for call in calls:
                    token.checkpoint()
                    result = await self._execute(call, token)
                    token.checkpoint()
                    content = ToolResultContent.model_validate_json(result.model_dump_json())
                    if content.call_id != call.call_id:
                        raise KernelError("tool_result_mismatch", "工具结果与调用 ID 不匹配")
                    if len(content.model_dump_json()) > turn.budget.max_output_chars:
                        raise KernelError("tool_output_too_large", "工具输出超过当前 Kernel 上限")
                    item_id = new_id()
                    await self._commit(
                        thread_id,
                        turn_id,
                        [
                            ItemStarted(item_id=item_id, content=content),
                            ItemFinished(
                                item_id=item_id, content=content, status=ItemStatus.COMPLETED
                            ),
                        ],
                    )
                    if content.outcome == "unknown":
                        raise KernelError("uncertain_effect", "工具结果未知，禁止继续模型循环")
            else:
                token.checkpoint()
                await self._state(thread_id, turn_id, TurnStatus.FINALIZING)
                return await self._finish(thread_id, turn_id, TurnStatus.COMPLETED, None)

    async def _execute(self, call: ToolCallContent, token: CancelToken) -> ToolResultContent:
        definition = self._definitions.get(call.tool)
        if definition is None:
            return ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                error=AgentFailure(code="unknown_tool", message="工具未注册"),
            )
        if definition.effect_class != EffectClass.READ_ONLY or definition.requires_approval:
            return ToolResultContent(
                call_id=call.call_id,
                outcome="failed",
                error=AgentFailure(
                    code="tool_not_enabled", message="当前 Kernel 切片未开放写工具或审批工具"
                ),
            )
        token.checkpoint()
        self._fault("runtime.before_tool")
        result = await token.run(self.tools.execute(call.model_copy(deep=True), token))
        self._fault("runtime.after_tool")
        return result

    async def _sample(self, request: ModelRequest, token: CancelToken) -> None:
        started = False
        completed: ResponseCompleted | None = None
        text_items: dict[str, tuple[UUID, str, bool]] = {}
        call_ids: set[str] = set()
        characters = 0
        stream_sequence = 0
        event_count = 0
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
                if isinstance(event, ResponseFailed):
                    raise KernelError("provider_" + event.code, "Provider 返回结构化失败")
                if isinstance(event, ResponseStarted):
                    if started:
                        raise KernelError("invalid_provider_output", "Provider 重复开始响应")
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
                    if event.finish_reason not in {"completed", "tool_calls"}:
                        raise KernelError("provider_" + event.finish_reason, "模型未正常完成")
                    if bool(call_ids) != (event.finish_reason == "tool_calls"):
                        raise KernelError("invalid_provider_output", "停止原因与 Tool Call 不一致")
                    if not call_ids and not any(part[1] for part in text_items.values()):
                        raise KernelError("invalid_provider_output", "模型响应没有语义内容")
                    completed = event
                    await self._commit(
                        request.thread_id,
                        request.turn_id,
                        [UsageRecorded(step=request.step, usage=event.usage)],
                    )
                else:
                    raise KernelError("invalid_provider_output", "不支持的 Provider 事件")
        if not started or completed is None:
            raise KernelError("provider_stream_incomplete", "Provider 流缺少完整终态")

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
            if turn.status == TurnStatus.CANCELLING and status != TurnStatus.INTERRUPTED:
                status = TurnStatus.CANCELLED
                error = AgentFailure(code="cancelled", message="取消已生效")
            if any(
                isinstance(i.content, ToolResultContent) and i.content.outcome == "unknown"
                for i in turn.items
            ) or any(c.effect_class != EffectClass.READ_ONLY for c in pending_calls(turn)):
                status = TurnStatus.INTERRUPTED
                error = AgentFailure(code="uncertain_effect", message="存在未知效果，禁止自动重放")
            payloads: list[EventPayload] = []
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
                # 恢复只结算事实，不调用 ToolRuntime，也不声称未知写操作已取消。
                result = ToolResultContent(
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
            payloads.append(TurnStateChanged(status=status, error=error))
            self._fault("runtime.before_terminal")
            thread = await self.store.append(
                thread_id,
                [EventDraft(turn_id=turn_id, payload=p) for p in payloads],
                expected_sequence=thread.sequence,
            )
            return get_turn(thread, turn_id)

    async def action_context(self, thread_id: UUID, turn_id: UUID) -> ActionContext:
        turn = get_turn(await self.store.get_thread(thread_id), turn_id)
        parts = turn.trace_context.traceparent.split("-") if turn.trace_context else []
        trace_id = parts[1] if len(parts) == 4 else None
        return ActionContext(session_id=str(thread_id), run_id=str(turn_id), trace_id=trace_id)
