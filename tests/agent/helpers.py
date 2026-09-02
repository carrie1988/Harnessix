from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import ToolCallContent, ToolResultContent
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.models.contracts import (
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)


def answer(text: str = "完成") -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="response"),
        TextStarted(content_id="answer"),
        TextDelta(content_id="answer", delta=text),
        TextCompleted(content_id="answer", text=text),
        ResponseCompleted(),
    ]


def tool_step(*names: str) -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="response"),
        *(
            ToolCallCompleted(call_id=f"call-{i}", tool=name, arguments={})
            for i, name in enumerate(names)
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


class RecordingTools:
    def __init__(
        self, *, effect: EffectClass = EffectClass.READ_ONLY, approval: bool = False
    ) -> None:
        self.effect = effect
        self.approval = approval
        self.calls: list[ToolCallContent] = []

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return (
            ToolDescriptor(
                name="test.read",
                version="1",
                description="只读测试工具",
                input_schema={"type": "object"},
                effect_class=self.effect,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=self.approval,
                supports_reconciliation=False,
            ),
        )

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        self.calls.append(call)
        return ToolResultContent(call_id=call.call_id, outcome="succeeded", output={"value": 1})
