"""在独立v14与当前wheel间验证摘要候选升级、窗口恢复及旧reader拒绝。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID, uuid4

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
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
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.context.compaction import plan_compaction, validate_compaction
from harnessix.context.compaction_contracts import (
    CompactionPolicy,
    CompactionSummary,
)
from harnessix.context.compaction_ledger_contracts import (
    CompactionAttemptFinished,
    CompactionAttemptStarted,
    CompactionPlanned,
    CompactionSummarized,
    CompactionUsageObserved,
)
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
)
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore


def state(path: Path) -> dict[str, list[list[object]]]:
    with sqlite3.connect(path) as database:
        return {
            table: [
                [value.hex() if isinstance(value, bytes) else value for value in row]
                for row in database.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("agent_events", "agent_threads", "agent_migrations")
        }


async def create_v14(store: SQLiteSessionStore, root: Path) -> None:
    assert EventDraft.model_fields["schema_version"].default == 14
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="offline"),
                TextStarted(content_id="answer"),
                TextCompleted(content_id="answer", text="解析器源码与恢复事实\n" * 1000),
                ResponseCompleted(),
            ]
        ]
    )
    async with AgentRuntime(store, provider) as runtime:
        thread = await runtime.create_thread(str(root))
        turn = await runtime.run_turn(thread.thread_id, "研究解析器", request_id="inspect")
        assert turn.status == TurnStatus.COMPLETED
    thread = await store.get_thread(thread.thread_id)
    turn_id, item_id = uuid4(), uuid4()
    content = TextContent(kind="user_message", text="继续修复解析器；保持public接口。")
    thread = await store.append(
        thread.thread_id,
        [
            EventDraft(turn_id=turn_id, payload=payload)
            for payload in (
                TurnStarted(
                    request_id="compact",
                    request_fingerprint="0" * 64,
                    budget=Budget(timeout_seconds=86_400),
                ),
                ItemStarted(item_id=item_id, content=content),
                ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
                TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
            )
        ],
        expected_sequence=thread.sequence,
    )
    prepared = await plan_compaction(
        thread,
        1,
        ToolResultViewPolicy(),
        CompactionPolicy(
            target_history_tokens=2500,
            summary_reserve_tokens=1024,
            max_summary_input_tokens=100_000,
            retain_recent_groups=1,
        ),
        CancelToken(),
        compaction_id=uuid4(),
    )
    summary = CompactionSummary(
        compaction_id=prepared.plan.compaction_id,
        text="解析器仍需修复；保持public接口并继续验证恢复路径。",
    )
    candidate = await validate_compaction(thread, prepared.plan, summary, CancelToken())
    attempt_id = uuid4()
    identity = prepared.plan.compaction_id
    snapshot = await store.append(
        thread.thread_id,
        [
            EventDraft(turn_id=thread.active_turn_id, payload=payload)
            for payload in (
                CompactionPlanned(
                    plan=prepared.plan, decisions=prepared.model_history.new_decisions
                ),
                CompactionAttemptStarted(
                    compaction_id=identity,
                    event=ModelAttemptStarted(
                        attempt_id=attempt_id,
                        step=1,
                        index=1,
                        provider="offline",
                        requested_model="summary",
                    ),
                ),
                CompactionUsageObserved(
                    compaction_id=identity,
                    event=ModelUsageObserved(
                        attempt_id=attempt_id,
                        actual_model="summary",
                        response_id="fixture",
                        usage=UsageObservation(
                            completeness="complete", input_tokens=100, output_tokens=30
                        ),
                    ),
                ),
                CompactionAttemptFinished(
                    compaction_id=identity,
                    event=ModelAttemptFinished(attempt_id=attempt_id, outcome="completed"),
                ),
                CompactionSummarized(
                    compaction_id=identity,
                    summary=summary,
                    candidate_history_sha256=candidate.history_sha256,
                    candidate_history_tokens=candidate.history_tokens,
                ),
            )
        ],
        expected_sequence=thread.sequence,
    )
    assert snapshot.turns[-1].compactions[0].status == "summarized"
    assert not hasattr(snapshot, "compaction_windows")
    (root / "metadata.json").write_text(
        json.dumps(
            {
                "thread_id": str(thread.thread_id),
                "compaction_id": str(identity),
                "state": state(store.path),
            },
            ensure_ascii=False,
        )
    )
    print("v14 wheel已创建完成摘要候选，未发布活动窗口")


async def main(mode: str, root: Path) -> None:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    store = SQLiteSessionStore(root / "session.sqlite")
    if mode == "create":
        await create_v14(store, root)
        return
    before = state(store.path)
    if mode == "old-reader":
        assert EventDraft.model_fields["schema_version"].default == 14
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("v14 reader错误接受migration17")
        assert state(store.path) == before
        print("v14 reader拒绝migration17且未修改数据库")
        return

    assert EventDraft.model_fields["schema_version"].default == 17
    metadata = json.loads((root / "metadata.json").read_text())
    original = metadata["state"]
    await store.initialize()
    migrated = state(store.path)
    assert migrated["agent_events"] == original["agent_events"]
    assert migrated["agent_threads"] == original["agent_threads"]
    assert migrated["agent_migrations"][:16] == original["agent_migrations"]
    assert len(migrated["agent_migrations"]) == 19
    thread_id = UUID(metadata["thread_id"])
    assert await store.get_thread(thread_id) == replay(await store.events(thread_id))
    if mode == "upgrade":
        print("v17 wheel仅追加migration17-19，v14事件和投影原字节保持不变")
        return

    assert mode == "recover"
    provider = FakeProvider()
    async with AgentRuntime(store, provider):
        recovered = await store.get_thread(thread_id)
    assert not provider.requests
    assert recovered == replay(await store.events(thread_id))
    assert recovered.turns[-1].status == TurnStatus.INTERRUPTED
    assert len(recovered.compaction_windows) == 1
    window = recovered.compaction_windows[0]
    assert str(window.compaction_id) == metadata["compaction_id"]
    assert recovered.active_compaction_window_id == window.window_id
    assert (
        sum(
            event.payload.type == "compaction_window_activated"
            for event in await store.events(thread_id)
        )
        == 1
    )
    after = state(store.path)
    assert after["agent_events"][: len(original["agent_events"])] == original["agent_events"]
    print("v15重开零Provider请求发布唯一窗口并以Interrupted关闭原Turn")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
