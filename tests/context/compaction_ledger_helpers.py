from __future__ import annotations

from uuid import uuid4

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.compaction import plan_compaction
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.models.scripted import ScriptedProvider
from tests.agent.helpers import answer
from tests.context.test_compaction import policy


async def append(store, thread, *payloads):
    return await store.append(
        thread.thread_id,
        [EventDraft(turn_id=thread.active_turn_id, payload=p) for p in payloads],
        expected_sequence=thread.sequence,
    )


async def prepare_source(store, workspace):
    async with AgentRuntime(
        store, ScriptedProvider([answer("解析器源码检查记录\n" * 1000)])
    ) as runtime:
        thread = await runtime.create_thread(str(workspace))
        await runtime.run_turn(thread.thread_id, "研究解析器", request_id="inspect")
    thread = await store.get_thread(thread.thread_id)
    turn_id, item_id = uuid4(), uuid4()
    content = TextContent(kind="user_message", text="继续修复解析器；保留public接口。")
    thread = await store.append(
        thread.thread_id,
        [
            EventDraft(turn_id=turn_id, payload=p)
            for p in (
                TurnStarted(request_id="compact", request_fingerprint="0" * 64, budget=Budget()),
                ItemStarted(item_id=item_id, content=content),
                ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
                TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
            )
        ],
        expected_sequence=thread.sequence,
    )
    prepared = await plan_compaction(
        thread, 1, ToolResultViewPolicy(), policy(), CancelToken(), compaction_id=uuid4()
    )
    return thread, prepared
