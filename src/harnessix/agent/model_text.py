"""Provider文本生命周期与公开投影；不承担模型尝试、工具调用或网络执行。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    EventPayload,
    ItemDelta,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    Thread,
)
from harnessix.agent.publication import (
    PublicOutputProtection,
    PublicTextStep,
    protect_json,
    protect_text,
)
from harnessix.models.contracts import ModelRequest, TextCompleted, TextDelta, TextStarted

Commit = Callable[[UUID, UUID, Sequence[EventPayload]], Awaitable[Thread]]


@dataclass
class _TextItem:
    item_id: UUID
    text: str = ""
    released: int = 0
    completed: bool = False


@dataclass
class ModelTextPublication:
    """保持原终值一致性及安全增量序号；未发布尾部仅存在当前步骤内存中。"""

    request: ModelRequest
    token: CancelToken
    protection: PublicTextStep | None
    commit: Commit
    emit: Callable[[ItemDelta], None]
    items: dict[str, _TextItem] = field(default_factory=dict)
    characters: int = 0
    sequence: int = 0

    def account(self, count: int) -> None:
        self.characters += count
        if self.characters > self.request.budget.max_output_chars:
            raise KernelError("model_output_too_large", "模型输出超过上限")

    async def accept(self, event: TextStarted | TextDelta | TextCompleted) -> None:
        if isinstance(event, TextStarted):
            await _start(self, event.content_id)
            return
        item = self.items.get(event.content_id)
        if item is None or item.completed:
            raise KernelError("invalid_provider_output", "文本块未开始或已结束")
        if isinstance(event, TextDelta):
            self.account(len(event.delta))
            item.text += event.delta
            prefix = await protect_text(self.protection, event.content_id, event.delta, self.token)
            self.publish(item, prefix)
        else:
            await _complete(self, item, event)

    def validate_prefix(self, item: _TextItem, prefix: str, *, final: bool = False) -> None:
        if not item.text.startswith(prefix, item.released) or (
            final and item.released + len(prefix) != len(item.text)
        ):
            raise KernelError("public_output_protection_failed", "公开文本保护改变原文")

    def publish(self, item: _TextItem, prefix: str) -> None:
        self.validate_prefix(item, prefix)
        item.released += len(prefix)
        if not prefix:
            return
        self.sequence += 1
        self.emit(
            ItemDelta(
                thread_id=self.request.thread_id,
                turn_id=self.request.turn_id,
                item_id=item.item_id,
                model_step=self.request.step,
                stream_sequence=self.sequence,
                delta=prefix,
            )
        )

    @property
    def all_completed(self) -> bool:
        return all(item.completed for item in self.items.values())

    @property
    def has_content(self) -> bool:
        return any(item.text for item in self.items.values())


async def _start(state: ModelTextPublication, content_id: str) -> None:
    if len(state.items) >= 128:
        raise KernelError("provider_item_limit", "模型步骤文本块数量超过上限")
    if content_id in state.items:
        raise KernelError("invalid_provider_output", "文本块 ID 重复")
    item = _TextItem(new_id())
    state.items[content_id] = item
    await state.commit(
        state.request.thread_id,
        state.request.turn_id,
        [ItemStarted(item_id=item.item_id, content=TextContent(kind="assistant_message"))],
    )


async def _complete(state: ModelTextPublication, item: _TextItem, event: TextCompleted) -> None:
    if item.text and item.text != event.text:
        raise KernelError("invalid_provider_output", "文本终值与增量不一致")
    prefix = ""
    if not item.text:
        state.account(len(event.text))
        item.text = event.text
        if state.protection is not None:
            prefix = await protect_text(state.protection, event.content_id, event.text, state.token)
    tail = await protect_text(state.protection, event.content_id, None, state.token)
    # 先校验完整终值和待提交事件，再发布最后尾部；无保护宿主保留仅终值无增量行为。
    state.validate_prefix(item, prefix + tail, final=state.protection is not None)
    await state.commit(
        state.request.thread_id,
        state.request.turn_id,
        [
            ItemFinished(
                item_id=item.item_id,
                status=ItemStatus.COMPLETED,
                content=TextContent(kind="assistant_message", text=event.text),
            )
        ],
    )
    state.publish(item, prefix + tail)
    item.completed = True


async def protect_request(
    request: ModelRequest, protection: PublicOutputProtection | None, token: CancelToken
) -> None:
    """主模型与摘要模型共用出站检查；无保护宿主不新增序列化工作。"""
    if protection is not None:
        await protect_json(protection, request.model_dump(mode="json"), token)
