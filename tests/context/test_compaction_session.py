from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

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
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.compaction import plan_compaction, validate_compaction
from harnessix.context.compaction_contracts import CompactionPlan, CompactionSummary
from harnessix.context.tool_result_contracts import ToolResultViewPolicy
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.models.contracts import ModelRequest
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import step
from tests.context.test_compaction import plan, policy, summary, text, thread_with, tool_group


async def test_real_file_session_plan_reopen_replay_and_both_provider_mappings(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source_file = root / "main.py"
    source_file.write_text(
        "# 保留接口\ndef parse(value):\n    return value\n" * 50, encoding="utf-8"
    )
    original_file_sha = hashlib.sha256(source_file.read_bytes()).hexdigest()
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([step("read_file", path="main.py"), answer("已读取源文件。")])
    async with CodingToolRuntime(root) as tools:
        async with AgentRuntime(store, provider, scoped_tools=tools) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "读取解析器", request_id="inspect")
            assert turn.status == TurnStatus.COMPLETED, turn.error
    snapshot = await store.get_thread(thread.thread_id)
    new_turn_id, user_id = uuid4(), uuid4()
    user = TextContent(kind="user_message", text="继续修复解析器；不得更改public接口。")
    budget = Budget()
    fingerprint = hashlib.sha256(
        json.dumps(
            {"prompt": user.text, "budget": budget.model_dump()},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    snapshot = await store.append(
        thread.thread_id,
        [
            EventDraft(turn_id=new_turn_id, payload=payload)
            for payload in (
                TurnStarted(request_id="continue", request_fingerprint=fingerprint, budget=budget),
                ItemStarted(item_id=user_id, content=user),
                ItemFinished(item_id=user_id, status=ItemStatus.COMPLETED, content=user),
                TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
            )
        ],
        expected_sequence=snapshot.sequence,
    )
    events_before = [event.model_dump_json() for event in await store.events(thread.thread_id)]
    prepared = await plan_compaction(
        snapshot, 1, ToolResultViewPolicy(), policy(), CancelToken(), compaction_id=uuid4()
    )
    assert prepared.plan.covered_item_ids
    assert not prepared.model_history.new_decisions
    plan_file = tmp_path / "candidate-plan.json"
    plan_file.write_text(prepared.plan.model_dump_json(), encoding="utf-8")
    reopened = SQLiteSessionStore(store.path)
    await reopened.initialize()
    restored = await reopened.get_thread(thread.thread_id)
    restored_plan = CompactionPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    candidate = await validate_compaction(
        restored,
        restored_plan,
        CompactionSummary(
            compaction_id=restored_plan.compaction_id,
            text="解析器源码已读取；尚未提交任何修改，需保持public接口并验证恢复路径。",
        ),
        CancelToken(),
    )
    request = ModelRequest(
        thread_id=restored.thread_id,
        turn_id=new_turn_id,
        step=1,
        history=candidate.history,
        tools=(),
        budget=budget,
    )
    chat, _ = chat_request(request, OpenAIChatConfig(model="offline-fixture"))
    anthropic, _ = anthropic_request(request, AnthropicConfig(model="offline-fixture"))
    assert chat["messages"][0]["role"] == chat["messages"][-1]["role"] == "user"
    assert anthropic["messages"][0]["role"] == anthropic["messages"][-1]["role"] == "user"
    assert chat["messages"][-1]["content"] == user.text
    assert anthropic["messages"][-1]["content"][-1]["text"] == user.text
    assert "system" not in anthropic
    assert all(message["role"] != "system" for message in chat["messages"])
    assert len(provider.requests) == 2
    assert (
        await reopened.rebuild(thread.thread_id)
        == snapshot
        == replay(await store.events(thread.thread_id))
    )
    assert [
        event.model_dump_json() for event in await store.events(thread.thread_id)
    ] == events_before
    assert all(event.schema_version == 14 for event in await store.events(thread.thread_id))
    assert hashlib.sha256(source_file.read_bytes()).hexdigest() == original_file_sha


@pytest.mark.parametrize(
    "mapping,config",
    [
        (chat_request, OpenAIChatConfig(model="offline-fixture")),
        (anthropic_request, AnthropicConfig(model="offline-fixture")),
    ],
)
async def test_retained_tool_group_and_summary_fit_existing_adapters(mapping, config):
    recent = tool_group(count=1, output={"status": "still_pending"})
    thread = thread_with((text("任务约束", "user_message"), *tool_group(), *recent))
    prepared = await plan(thread, target_history_tokens=3500)
    candidate = await validate_compaction(thread, prepared.plan, summary(prepared), CancelToken())
    assert {entry.item_id for entry in recent} <= set(prepared.plan.retained_item_ids)
    body, _ = mapping(
        ModelRequest(
            thread_id=thread.thread_id,
            turn_id=thread.active_turn_id,
            step=1,
            history=candidate.history,
            tools=(),
            budget=Budget(),
        ),
        config,
    )
    wire = json.dumps(body, ensure_ascii=False)
    assert "still_pending" in wire
    assert "derived_history" in wire
    assert body["messages"][0]["role"] == "user"
