"""calling_model内模型尝试事实的提交时机（ADR 0168 D3）：成功尝试的终态事实与响应终态合并提交。

职责：决定模型尝试事实（尝试开始、用量、尝试结束）与响应终态（UsageRecorded）各在哪次提交里落盘。
“尝试开始”始终单独、最先提交；成功尝试的完整用量与“尝试完成”先留在内存，与响应终态同一事务提交。
在此之前，若收到其他Provider事件（正文、思考、下一次尝试、失败等）或处理以任何方式结束，先按原顺序
把留存的事实提交。每个事实的公开输出检查仍在收到时单独进行，与原来逐个提交时的调用相同。
非职责：不校验Provider事件顺序、不做预算判断、不发起模型请求、不决定事实的内容。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field

from harnessix.agent.errors import KernelError
from harnessix.agent.models import EventPayload, Thread, UsageRecorded
from harnessix.agent.usage import ModelAttemptFinished, ModelAttemptStarted, ModelUsageObserved
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ToolCallCompleted,
    ToolCallRejected,
)

AttemptFact = ModelAttemptStarted | ModelUsageObserved | ModelAttemptFinished


def holds(event: ProviderEvent) -> bool:
    """成功尝试的完整用量与“尝试完成”：后面紧跟的就是本次响应的终态，可以与它一起提交。"""
    if isinstance(event, ModelUsageObserved):
        return event.usage.completeness == "complete"
    return isinstance(event, ModelAttemptFinished) and event.outcome == "completed"


def _passes_held_facts(event: ProviderEvent) -> bool:
    """处理时不提交任何事实、可以跨过留存事实的事件：工具调用提案只在内存中收集，响应终态与它们合并。"""
    return holds(event) or isinstance(
        event, ToolCallCompleted | ToolCallRejected | ResponseCompleted
    )


@dataclass
class AttemptFactCommits:
    """一个模型步骤内的模型尝试事实提交。

    protect：一个事实的公开输出检查（与原来单独提交时的调用相同）；append：已检查的事实在Thread锁内
    CAS追加；durable：事实落盘之后的处理（尝试状态、用量遥测、故障注入点），按事实顺序逐个调用。
    """

    protect: Callable[[Sequence[EventPayload]], Awaitable[None]]
    append: Callable[[Sequence[EventPayload]], Awaitable[Thread]]
    durable: Callable[[AttemptFact, Thread], None]
    held: list[AttemptFact] = field(default_factory=list)

    async def flushing(
        self, events: AsyncGenerator[ProviderEvent, None]
    ) -> AsyncGenerator[ProviderEvent, None]:
        """Provider事件原样传下去。事件流结束或被关闭时（正常结束、异常或取消离开），先提交留存的
        事实，再关闭Provider的流；提交失败的错误优先抛出，因为按原顺序它本来发生在先。"""
        async with aclosing(events) as source:
            try:
                async for event in source:
                    yield event
            finally:
                await self.flush()

    async def before(self, event: ProviderEvent) -> None:
        """处理下一个Provider事件之前：它不能跨过留存的事实时，先把它们提交。"""
        if self.held and not _passes_held_facts(event):
            await self.flush()

    async def record(self, fact: AttemptFact) -> None:
        """检查并提交一个模型尝试事实；成功尝试的终态事实先留存，等响应终态一起提交。"""
        await self.protect([fact])
        if holds(fact):
            self.held.append(fact)
            return
        await self.flush()
        self.durable(fact, await self._append_fact(fact))

    async def commit_with_terminal(self, terminal: UsageRecorded) -> Thread:
        """响应终态与留存的事实同一事务提交；终态本身的错误由调用方按原规则映射。"""
        await self.protect([terminal])
        held, self.held = self.held, []
        if not held:
            return await self.append([terminal])
        try:
            snapshot = await self.append([*held, terminal])
        except KernelError as error:
            if error.code != "invalid_event":
                raise
            # 合并批次被Reducer拒绝：按原来的逐个提交重做，得到原来的持久结果与错误。
            await self._append_each(held)
            return await self.append([terminal])
        for fact in held:
            self.durable(fact, snapshot)
        return snapshot

    async def flush(self) -> None:
        """按原顺序把留存的事实作为一个批次提交。"""
        held, self.held = self.held, []
        if not held:
            return
        try:
            snapshot = await self.append(held)
        except KernelError as error:
            if error.code != "invalid_event":
                raise
            await self._append_each(held)
            return
        for fact in held:
            self.durable(fact, snapshot)

    async def _append_each(self, facts: Sequence[AttemptFact]) -> None:
        for fact in facts:
            self.durable(fact, await self._append_fact(fact))

    async def _append_fact(self, fact: AttemptFact) -> Thread:
        try:
            return await self.append([fact])
        except KernelError as error:
            if error.code == "invalid_event":
                raise KernelError("invalid_provider_output", "模型尝试事实不符合契约") from None
            raise
