from harnessix.agent.ids import new_id
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ThreadCreated,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
)
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.models.contracts import ProviderEvent, ResponseCompleted, ResponseStarted
from tests.agent.helpers import answer


def attempt_start(*, step: int = 1, index: int = 1) -> ModelAttemptStarted:
    return ModelAttemptStarted(
        attempt_id=new_id(),
        step=step,
        index=index,
        provider="fixture",
        requested_model="fixture-model",
    )


def observed(start: ModelAttemptStarted, **counts) -> ModelUsageObserved:
    return ModelUsageObserved(
        attempt_id=start.attempt_id,
        actual_model="fixture-model-v1",
        response_id=f"response-{start.index}",
        usage=UsageObservation(**counts),
    )


def accounted_answer(*, start: ModelAttemptStarted | None = None) -> list[ProviderEvent]:
    start = start or attempt_start()
    events = answer()
    events[0] = ResponseStarted(response_id=f"response-{start.index}")
    return [
        start,
        *events[:-2],
        observed(
            start,
            completeness="complete",
            input_tokens=10,
            output_tokens=3,
            cache_read_input_tokens=4,
            reasoning_output_tokens=1,
        ),
        ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        events[-2],
        ResponseCompleted(usage=Usage(input_tokens=10, output_tokens=3)),
    ]


async def prepare_attempt(store, workspace):
    await store.initialize()
    thread_id, turn_id, item_id = new_id(), new_id(), new_id()
    thread = await store.append(
        thread_id,
        [EventDraft(payload=ThreadCreated(workspace=str(workspace)))],
        expected_sequence=0,
    )
    content = TextContent(kind="user_message", text="进程崩溃验收")
    payloads = [
        TurnStarted(request_id="crash", request_fingerprint="0" * 64, budget=Budget()),
        ItemStarted(item_id=item_id, content=content),
        ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
        TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
        TurnStateChanged(status=TurnStatus.CALLING_MODEL),
    ]
    return await store.append(
        thread_id,
        [EventDraft(turn_id=turn_id, payload=p) for p in payloads],
        expected_sequence=thread.sequence,
    )
