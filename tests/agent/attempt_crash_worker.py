from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from harnessix.agent.billing import ResponseBillingMetadata
from harnessix.agent.models import EventDraft
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished
from harnessix.models.scripted import FakeProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import accounted_answer, attempt_start, observed


async def main() -> None:
    database, thread_id, mode, point = sys.argv[1:]

    def crash(name):
        if name == point:
            os._exit(77)

    store = SQLiteSessionStore(database, fault=crash if mode != "runtime" else None)
    if mode == "runtime":
        events = accounted_answer()

        class Provider:
            async def stream(self, request, cancel):
                yield events[0]
                await asyncio.to_thread(Path(database + ".requests").write_text, "1")
                for event in events[1:]:
                    yield event

        async with AgentRuntime(store, Provider(), fault=crash) as runtime:
            await runtime.run_turn(UUID(thread_id), "运行时切点", request_id="runtime")
    elif mode == "recovery":
        async with AgentRuntime(store, FakeProvider()):
            pass
    else:
        thread = await store.get_thread(UUID(thread_id))
        start = attempt_start()
        if thread.turns[-1].model_attempts:
            start = start.model_copy(
                update={"attempt_id": thread.turns[-1].model_attempts[0].attempt_id}
            )
        payload = {
            "start": start,
            "usage": observed(start, completeness="complete", input_tokens=10, output_tokens=3),
            "billing": observed(
                start,
                completeness="complete",
                input_tokens=10,
                output_tokens=3,
                cache_creation_input_tokens=3,
            ).model_copy(
                update={
                    "billing": ResponseBillingMetadata(
                        service_tier="standard",
                        cache_creation_5m_tokens=3,
                        cache_creation_1h_tokens=0,
                    )
                }
            ),
            "finish": ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed"),
        }[mode]
        await store.append(
            thread.thread_id,
            [EventDraft(turn_id=thread.active_turn_id, payload=payload)],
            expected_sequence=thread.sequence,
        )
    raise AssertionError("未触发崩溃切点")


if __name__ == "__main__":
    asyncio.run(main())
