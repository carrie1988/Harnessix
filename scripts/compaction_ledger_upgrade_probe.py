"""在独立v13与当前wheel环境验证原字节升级、账本追加与旧reader拒绝。"""

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
from harnessix.context.compaction import plan_compaction, validate_compaction
from harnessix.context.compaction_contracts import CompactionPolicy, CompactionSummary
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
)
from harnessix.models.scripted import FakeProvider, ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore


def state(path):
    with sqlite3.connect(path) as database:
        return {
            table: database.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in ("agent_events", "agent_threads", "agent_migrations")
        }


async def main(mode, root):
    root.mkdir(parents=True, exist_ok=True)
    store = SQLiteSessionStore(root / "session.sqlite")
    metadata = root / "metadata.json"
    if mode == "create":
        assert EventDraft.model_fields["schema_version"].default == 13
        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="offline"),
                    TextStarted(content_id="answer"),
                    TextCompleted(content_id="answer", text="源码研究与失败路径\n" * 1000),
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
                EventDraft(turn_id=turn_id, payload=p)
                for p in (
                    TurnStarted(
                        request_id="summary",
                        request_fingerprint="0" * 64,
                        budget=Budget(timeout_seconds=86400),
                    ),
                    ItemStarted(item_id=item_id, content=content),
                    ItemFinished(item_id=item_id, content=content, status=ItemStatus.COMPLETED),
                    TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
                )
            ],
            expected_sequence=thread.sequence,
        )
        metadata.write_text(
            json.dumps({"thread_id": str(thread.thread_id), "state": state(store.path)})
        )
        print("v13基础wheel已建立离线长历史，当前Turn尚无摘要请求")
        return
    before = state(store.path)
    if mode == "old-reader":
        assert EventDraft.model_fields["schema_version"].default == 13
        try:
            await store.initialize()
        except KernelError as error:
            assert error.code == "schema_too_new"
        else:
            raise AssertionError("旧reader错误接受migration16及后续版本")
        assert before == state(store.path)
        print("v13 reader拒绝migration16及后续版本且未修改数据库")
        return
    assert EventDraft.model_fields["schema_version"].default == 18
    original = json.loads(metadata.read_text())
    await store.initialize()
    after = state(store.path)
    assert after["agent_events"] == before["agent_events"]
    assert after["agent_threads"] == before["agent_threads"]
    assert after["agent_migrations"][:15] == before["agent_migrations"][:15]
    assert len(after["agent_migrations"]) == 19
    thread_id = UUID(original["thread_id"])
    source = await store.get_thread(thread_id)
    assert source == replay(await store.events(thread_id))
    if mode == "upgrade":
        assert not source.turns[-1].compactions
        print("当前wheel仅追加migration16-19；v13历史原字节保留且Replay一致")
        return
    assert mode == "append"
    from harnessix.agent.usage import (
        ModelAttemptFinished,
        ModelAttemptStarted,
        ModelUsageObserved,
        UsageObservation,
    )
    from harnessix.context.compaction_ledger_contracts import (
        CompactionAttemptFinished,
        CompactionAttemptStarted,
        CompactionPlanned,
        CompactionSummarized,
        CompactionUsageObserved,
    )
    from harnessix.models.costs import COST_REPORT_ADAPTER, build_cost_report

    prepared = await plan_compaction(
        source,
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
        compaction_id=prepared.plan.compaction_id, text="解析器仍需修复；保持public接口。"
    )
    candidate = await validate_compaction(source, prepared.plan, summary, CancelToken())
    attempt_id, identity = uuid4(), prepared.plan.compaction_id
    snapshot = await store.append(
        thread_id,
        [
            EventDraft(turn_id=source.active_turn_id, payload=p)
            for p in (
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
        expected_sequence=source.sequence,
    )
    assert snapshot.turns[-1].usage.total_tokens == 130
    assert snapshot.turns[-1].model_steps == 0
    assert snapshot.turns[-1].items == source.turns[-1].items
    assert (
        state(store.path)["agent_events"][: len(before["agent_events"])] == before["agent_events"]
    )
    assert await store.rebuild(thread_id) == snapshot
    provider = FakeProvider()
    async with AgentRuntime(SQLiteSessionStore(store.path), provider):
        pass
    assert not provider.requests
    restored = await store.get_thread(thread_id)
    assert restored.turns[-1].status == TurnStatus.INTERRUPTED
    assert restored.turns[-1].compactions[0].status == "summarized"
    report = build_cost_report(restored.turns[-1])
    assert report.spec_version == "harnessix.cost-report/v2"
    assert report.summary.completeness == "unknown"  # 没有价格证据，不补零费用。
    assert COST_REPORT_ADAPTER.validate_json(report.model_dump_json()) == report
    print("当前摘要账本追加、候选持久化、费用未知标志与重开零请求全部通过")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
