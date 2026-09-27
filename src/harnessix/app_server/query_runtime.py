"""只读查询的原输入、原DTO与错误公开边界，以及无持久副作用的事件长轮询。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.command_runtime import AgentServiceError, service_error
from harnessix.protocol.contracts import (
    EventsNextParams,
    EventsNextResult,
    EventsReplayParams,
    EventsReplayResult,
    ProtocolModel,
)
from harnessix.protocol.projection import project_replay
from harnessix.session.ports import SessionStore


class UnexpectedQueryError(AgentServiceError):
    """查询宿主意外失败的固定公开错误；Server仍使用既有JSON-RPC内部错误分类。"""

    def __init__(self) -> None:
        super().__init__("internal_error", "服务端内部错误；原始异常未公开")


async def execute_query[Params: ProtocolModel, Result: ProtocolModel](
    runtime: AgentRuntime,
    params: Params,
    operation: Callable[[], Awaitable[Result]],
    *,
    after_result: Callable[[Result], None] | None = None,
) -> Result:
    """先准入完整原参数，后保护原结果；安全结果之前不得执行恢复调度回调。"""
    try:
        await runtime.validate_public_input(
            params.model_dump(mode="json", by_alias=True, warnings="error")
        )
    except KernelError as error:
        raise service_error(error) from None
    except Exception:
        raise AgentServiceError("invalid_params", "协议参数无效") from None
    try:
        result = await operation()
        await runtime.validate_public_output(
            result.model_dump(mode="json", by_alias=True, warnings="error")
        )
        if after_result is not None:
            after_result(result)
        return result
    except (KernelError, AgentServiceError) as error:
        failure = error if isinstance(error, AgentServiceError) else service_error(error)
        try:
            await runtime.validate_public_output(
                {
                    "code": failure.code,
                    "message": failure.message,
                    "retryable": failure.retryable,
                }
            )
        except KernelError as protection_error:
            failure = service_error(protection_error)
        raise failure from None
    except Exception:
        raise UnexpectedQueryError() from None


async def replay_snapshot(store: SessionStore, params: EventsReplayParams) -> EventsReplayResult:
    """构造原持久Replay候选；仅内部复用，候选不得绕过execute_query公开。"""
    events = await store.events(params.thread_id, after=params.after_cursor)
    page = events[: params.limit]
    scanned = page[-1].sequence if page else params.after_cursor
    return project_replay(
        params.thread_id,
        page,
        scanned_through=scanned,
        has_more=len(events) > len(page),
    )


async def poll_events(
    params: EventsNextParams,
    *,
    signal: asyncio.Event,
    snapshot: Callable[[], Awaitable[EventsNextResult | None]],
    replay: Callable[[], Awaitable[EventsReplayResult]],
    closed: Callable[[], bool],
) -> EventsNextResult:
    """原50ms重读、清信号竞态防护和关闭唤醒；公开保护由外层查询边界统一执行。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + params.wait_ms / 1000
    while True:
        ready = await snapshot()
        if ready is not None:
            return ready
        remaining = deadline - loop.time()
        if remaining <= 0 or closed():
            break
        signal.clear()
        # clear与wait之间重新读取，避免Delta恰在注册窗口到达而沉睡；
        # 50ms轮询观察不产生Delta的审批、提问、Tool和终态事件。
        ready = await snapshot()
        if ready is not None:
            return ready
        try:
            async with asyncio.timeout(min(0.05, remaining)):
                await signal.wait()
        except TimeoutError:
            pass
    ready = await snapshot()
    if ready is not None:
        return ready
    final = await replay()
    if final.scanned_through > params.after_cursor or final.has_more:
        return EventsNextResult(replay=final)
    return EventsNextResult(replay=final, timed_out=True)
