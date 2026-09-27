"""用户问答的确定性事件构造；不读写Store、不判定状态、不执行工具。"""

from uuid import UUID, uuid5

from harnessix.agent.models import (
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    QuestionAnswerContent,
    ToolResultContent,
    TurnStateChanged,
    TurnStatus,
)


def question_answer_events(
    turn_id: UUID, question_id: UUID, call_id: UUID, answer: str
) -> list[EventDraft]:
    """生成同一事务的回答与Tool Result；调用方先核对问题、身份和原输入保护。"""
    answer_content = QuestionAnswerContent(question_id=question_id, call_id=call_id, answer=answer)
    result = ToolResultContent(call_id=call_id, outcome="succeeded", output={"answer": answer})
    answer_item_id = uuid5(question_id, "harnessix.question-answer/v1")
    result_item_id = uuid5(question_id, "harnessix.question-result/v1")
    return [
        EventDraft(
            turn_id=turn_id, payload=ItemStarted(item_id=answer_item_id, content=answer_content)
        ),
        EventDraft(
            turn_id=turn_id,
            payload=ItemFinished(
                item_id=answer_item_id, content=answer_content, status=ItemStatus.COMPLETED
            ),
        ),
        EventDraft(turn_id=turn_id, payload=TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS)),
        EventDraft(turn_id=turn_id, payload=ItemStarted(item_id=result_item_id, content=result)),
        EventDraft(
            turn_id=turn_id,
            payload=ItemFinished(
                item_id=result_item_id, content=result, status=ItemStatus.COMPLETED
            ),
        ),
    ]
