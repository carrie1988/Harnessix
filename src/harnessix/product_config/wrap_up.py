"""收尾：用户要求收尾之后不再发起新的修改或检查，模型只能给出总结（Beta Issue #4）。

一键收尾（Ctrl+F）与运行时间检查点的“收尾”都替用户发一段固定说明。0.10.0b24：说明已经进入会话，
模型仍又读了README、跑了两条命令检查，53秒后才结束——说明只在下一步被模型看到，模型也可以不照做。
现在由宿主保证：说明一旦记入本轮，尚未开始的命令、修改等经审批边界的调用不再执行（如实告诉模型），
之后的模型步骤不提供任何工具；同一步里已提出的只读读取照常完成（毫秒级，不经审批边界）。
判定只看本轮已持久化的用户消息，恢复重放得到相同结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harnessix.agent.errors import AgentFailure
from harnessix.agent.models import TextContent, ToolCallContent, Turn
from harnessix.agent.ports import ModelToolCallAdmission, ModelToolSelection, ModelToolView
from harnessix.domain.models import ToolDescriptor

# 运行中的一轮按Ctrl+F，或检查点询问时选“收尾”。
WRAP_UP_STEER: Final = (
    "请现在收尾：不要再做新的修改或检查，直接给出最终总结——已经完成了什么、"
    "哪些没有来得及做、哪些你没能验证需要我自己确认。"
)
# 检查点暂停之后选“收尾”：开新的一轮。
WRAP_UP_PROMPT: Final = (
    "上一轮已暂停。请不要再做新的修改或检查，直接给出最终总结："
    "已经完成了什么、哪些没有来得及做、哪些你没能验证需要我自己确认。"
)
WRAP_UP_TEXTS: Final = frozenset({WRAP_UP_STEER, WRAP_UP_PROMPT})
WRAP_UP_REFUSAL: Final = AgentFailure(
    code="tool_wrap_up_requested",
    message="你已要求收尾，这次调用没有执行；请直接给出最终总结",
)


def wrap_up_requested(turn: Turn) -> bool:
    """本轮是否已记入收尾说明（用户消息与运行中补充的说明都算）。"""

    return any(
        isinstance(item.content, TextContent)
        and item.content.kind == "user_message"
        and item.content.text in WRAP_UP_TEXTS
        for item in turn.items
    )


@dataclass(frozen=True, slots=True)
class WrapUpToolView:
    """收尾之后不提供工具、不准入尚未开始的调用；之前照原视图，没有原视图时提供全部工具。"""

    inner: ModelToolView | None = None

    def select(
        self, turn: Turn, definitions: tuple[ToolDescriptor, ...]
    ) -> ModelToolSelection | tuple[ToolDescriptor, ...]:
        if wrap_up_requested(turn):
            return ModelToolSelection(())
        if self.inner is None:
            return ModelToolSelection(definitions)
        return self.inner.select(turn, definitions)

    def admit_call(self, turn: Turn, call: ToolCallContent) -> AgentFailure | None:
        if wrap_up_requested(turn):
            return WRAP_UP_REFUSAL
        if isinstance(self.inner, ModelToolCallAdmission):
            return self.inner.admit_call(turn, call)
        return None
