"""真实SQLite、SDK即时通知与重放、模型历史及尝试账本的持久前保护。"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from harnessix.agent.models import TurnStatus, Usage
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.usage import ModelAttemptFinished, ModelUsageObserved, UsageObservation
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.attempt_helpers import attempt_start
from tests.agent.helpers import RecordingTools, answer
from tests.agent.test_publication import CANARY, protected
from tests.agent.test_telemetry import instrumented
from tests.context.test_compaction_runtime import SequenceProvider, accounted_text, config


def chunks(text, *, final_only=False):
    return [
        ResponseStarted(response_id="response"),
        TextStarted(content_id="answer"),
        *(
            []
            if final_only
            else [TextDelta(content_id="answer", delta=c) for c in (text[:5], "", text[5:])]
        ),
        TextCompleted(content_id="answer", text=text),
        ResponseCompleted(),
    ]


@pytest.mark.parametrize("case", ["safe", "raw", "split_base64", "final_only", "interleaved"])
async def test_direct_model_text_persistence_replay_next_history_and_telemetry(tmp_path, case):
    safe = "检查源码并完成恢复测试。" * 30
    text = (
        safe
        if case == "safe"
        else base64.b64encode(CANARY.encode()).decode()
        if case == "split_base64"
        else CANARY
    )
    events = chunks(text, final_only=case == "final_only")
    if case == "interleaved":
        events.insert(3, TextStarted(content_id="other"))
        events.insert(4, TextDelta(content_id="other", delta=safe))
        events.insert(5, TextCompleted(content_id="other", text=safe))
    provider = SequenceProvider([events, answer("后续安全回答")])
    store = SQLiteSessionStore(tmp_path / "s.db")
    observer, exporter, reader = instrumented()
    deltas = []
    with protected() as scope:
        try:
            async with AgentRuntime(
                store,
                provider,
                public_output_protection=scope,
                observability=observer,
                on_delta=deltas.append,
            ) as agent:
                thread = await agent.create_thread(str(tmp_path))
                first = await agent.run_turn(thread.thread_id, "检查模型出口", request_id="first")
                assert first.status is (
                    TurnStatus.COMPLETED if case == "safe" else TurnStatus.FAILED
                )
                if case != "safe":
                    assert first.error.code == "public_output_secret_leak"
                assert await agent.resume_turn(thread.thread_id, first.turn_id) == first
                client = AgentClient(
                    InProcessAgentTransport(
                        AgentProtocolServer(
                            AgentApplicationService(
                                agent, store, SQLiteProtocolRequestStore(store.path)
                            )
                        )
                    )
                )
                await client.initialize()
                try:
                    replay = await client.replay_events(thread.thread_id, limit=200)
                    page = await client.next_events(
                        thread.thread_id, after_cursor=replay.scanned_through, wait_ms=0
                    )
                    second = await agent.run_turn(
                        thread.thread_id, "继续安全请求", request_id="second"
                    )
                    assert second.status is TurnStatus.COMPLETED
                finally:
                    await client.close()
                public = [
                    first.model_dump_json(),
                    replay.model_dump_json(),
                    page.model_dump_json(),
                    second.model_dump_json(),
                    (await store.get_thread(thread.thread_id)).model_dump_json(),
                    *(d.model_dump_json() for d in deltas),
                    *(r.model_dump_json() for r in provider.requests),
                    *(s.to_json() for s in exporter.get_finished_spans()),
                    reader.get_metrics_data().to_json(),
                ]
                assert all(
                    CANARY not in p and (case != "split_base64" or text not in p) for p in public
                )
                assert len(provider.requests) == provider.closed_streams == 2
                if case == "safe":
                    assert "".join(d.delta for d in deltas if d.turn_id == first.turn_id) == safe
                assert [d.stream_sequence for d in deltas if d.turn_id == first.turn_id] == list(
                    range(1, sum(d.turn_id == first.turn_id for d in deltas) + 1)
                )
            assert all(CANARY.encode() not in p.read_bytes() for p in tmp_path.glob("*.db*"))
        finally:
            observer.close()


async def test_actual_sdk_receives_safe_prefix_before_model_completes_and_never_secret_tail(
    tmp_path,
):
    ready, release = asyncio.Event(), asyncio.Event()

    class Provider:
        closed = False

        async def stream(self, request, token):
            try:
                yield ResponseStarted(response_id="r")
                yield TextStarted(content_id="a")
                yield TextDelta(content_id="a", delta="x" * 128 + CANARY[:8])
                ready.set()
                await token.run(release.wait())
                yield TextDelta(content_id="a", delta=CANARY[8:])
                yield TextCompleted(content_id="a", text="x" * 128 + CANARY)
                yield ResponseCompleted()
            finally:
                self.closed = True

    provider = Provider()
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        async with AgentRuntime(store, provider, public_output_protection=scope) as agent:
            thread = await agent.create_thread(str(tmp_path))
            client = AgentClient(
                InProcessAgentTransport(
                    AgentProtocolServer(
                        AgentApplicationService(
                            agent, store, SQLiteProtocolRequestStore(store.path)
                        )
                    )
                )
            )
            await client.initialize()
            task = asyncio.create_task(
                agent.run_turn(thread.thread_id, "流式校验", request_id="live")
            )
            try:
                async with asyncio.timeout(5):
                    await ready.wait()
                    snapshot = await store.get_thread(thread.thread_id)
                    page = await client.next_events(
                        thread.thread_id, after_cursor=snapshot.sequence, wait_ms=0
                    )
                    assert page.deltas and not task.done()
                    assert "".join(d.delta for d in page.deltas) and all(
                        set(d.delta) == {"x"} for d in page.deltas
                    )
                    release.set()
                    result = await task
                assert result.error.code == "public_output_secret_leak"
                replay = await client.replay_events(thread.thread_id, limit=200)
                assert CANARY not in replay.model_dump_json() and provider.closed
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await client.close()


@pytest.mark.parametrize("field", ["arguments", "provider_call_id", "tool"])
async def test_tool_call_publication_rejects_before_persistence_or_execution(tmp_path, field):
    tools = RecordingTools()
    data = dict(call_id="r", tool="test.read", arguments={"value": "safe"})
    if field == "arguments":
        data["arguments"] = {"value": CANARY}
    elif field == "provider_call_id":
        data["call_id"] = CANARY
    else:
        data["tool"] = CANARY
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="r"),
                ToolCallCompleted(**data),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer(),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        async with AgentRuntime(store, provider, tools, public_output_protection=scope) as agent:
            thread = await agent.create_thread(str(tmp_path))
            result = await agent.run_turn(thread.thread_id, "参数保护", request_id="call")
            if field == "tool":
                # 未登记名称不进入持久化：只留下不可执行的目录拒绝与固定失败反馈。
                from harnessix.agent.models import ToolCallRejectionContent

                assert result.status is TurnStatus.COMPLETED and result.error is None
                assert any(isinstance(i.content, ToolCallRejectionContent) for i in result.items)
            else:
                assert result.error.code == "public_output_secret_leak"
            assert not tools.calls
            assert CANARY not in result.model_dump_json()
            assert CANARY not in (await store.get_thread(thread.thread_id)).model_dump_json()


@pytest.mark.parametrize("case", ["safe", "rejected_text", "metadata"])
async def test_attempt_intent_precedes_transport_and_usage_keeps_committed_facts(tmp_path, case):
    start = attempt_start()
    store = SQLiteSessionStore(tmp_path / "s.db")
    saw_intent = []

    class Provider:
        async def stream(self, request, token):
            yield start
            persisted = await store.get_thread(request.thread_id)
            assert persisted.turns[-1].model_attempts[-1].attempt_id == start.attempt_id
            saw_intent.append(True)
            yield ResponseStarted(response_id="response-1")
            yield ModelUsageObserved(
                attempt_id=start.attempt_id,
                usage=UsageObservation(completeness="partial", input_tokens=10, output_tokens=1),
                actual_model=CANARY.encode().hex() if case == "metadata" else "safe-model",
                response_id="response-1",
            )
            if case == "metadata":
                return
            yield TextStarted(content_id="a")
            text = CANARY if case == "rejected_text" else "safe final"
            yield TextDelta(content_id="a", delta=text)
            yield TextCompleted(content_id="a", text=text)
            yield ModelUsageObserved(
                attempt_id=start.attempt_id,
                usage=UsageObservation(completeness="complete", input_tokens=10, output_tokens=3),
            )
            yield ModelAttemptFinished(attempt_id=start.attempt_id, outcome="completed")
            yield ResponseCompleted(usage=Usage(input_tokens=10, output_tokens=3))

    with protected() as scope:
        async with AgentRuntime(store, Provider(), public_output_protection=scope) as agent:
            thread = await agent.create_thread(str(tmp_path))
            result = await agent.run_turn(thread.thread_id, "请求意图", request_id="attempt")
            assert saw_intent == [True]
            assert result.status is (
                TurnStatus.COMPLETED if case == "safe" else TurnStatus.FAILED
            ), result.error
            assert result.usage == Usage(
                input_tokens=0 if case == "metadata" else 10,
                output_tokens=0 if case == "metadata" else 3 if case == "safe" else 1,
            )
            assert result.model_attempts[0].status == ("completed" if case == "safe" else "failed")
            assert CANARY not in (await store.get_thread(thread.thread_id)).model_dump_json()


async def test_registered_legacy_history_is_rejected_before_next_provider_but_not_rewritten(
    tmp_path,
):
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([answer(CANARY)])) as seed:
        thread = await seed.create_thread(str(tmp_path))
        legacy = await seed.run_turn(thread.thread_id, "历史材料", request_id="legacy")
    before = hashlib.sha256(legacy.model_dump_json().encode()).hexdigest()
    provider = ScriptedProvider([answer("safe")])
    with protected() as scope:
        async with AgentRuntime(store, provider, public_output_protection=scope) as agent:
            result = await agent.run_turn(thread.thread_id, "后续请求", request_id="protected")
            assert result.error.code == "public_output_secret_leak" and not provider.requests
            old = (await store.get_thread(thread.thread_id)).turns[0]
            assert hashlib.sha256(old.model_dump_json().encode()).hexdigest() == before
            assert (
                CANARY in old.model_dump_json()
            )  # 历史授权/迁移仍是独立缺口，不伪装为历史治理完成。


@pytest.mark.parametrize("case", ["safe", "summary_leak"])
async def test_compaction_summary_persisted_before_activation_is_also_protected(tmp_path, case):
    normal = SequenceProvider(
        [accounted_text("调查事实与恢复语义。\n" * 900), accounted_text("后续完成")]
    )
    summary = ScriptedProvider(
        [accounted_text(CANARY if case == "summary_leak" else "保留工程事实与边界")]
    )
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        async with AgentRuntime(
            store,
            normal,
            compaction=config(),
            summary_provider=summary,
            public_output_protection=scope,
        ) as agent:
            thread = await agent.create_thread(str(tmp_path))
            first = await agent.run_turn(thread.thread_id, "调查", request_id="seed")
            assert first.status is TurnStatus.COMPLETED
            result = await agent.run_turn(thread.thread_id, "继续", request_id="follow")
            assert summary.requests
            assert result.status is (TurnStatus.COMPLETED if case == "safe" else TurnStatus.FAILED)
            if case == "summary_leak":
                assert result.error.code == "public_output_secret_leak"
                assert result.compactions[0].status == "failed"
                assert not (await store.get_thread(thread.thread_id)).compaction_windows
            assert CANARY not in (await store.get_thread(thread.thread_id)).model_dump_json()


@pytest.mark.parametrize("case", ["parent", "token"])
async def test_runtime_cancellation_closes_provider_and_pending_secret_window(
    tmp_path, monkeypatch, case
):
    ready = asyncio.Event()
    step_refs = []

    class Provider:
        closed = False

        async def stream(self, request, token):
            try:
                yield ResponseStarted(response_id="r")
                yield TextStarted(content_id="a")
                yield TextDelta(content_id="a", delta=CANARY[:8])
                ready.set()
                await token.run(asyncio.Event().wait())
            finally:
                self.closed = True

    provider = Provider()
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        factory = scope.begin_public_text_step

        def begin(**kwargs):
            step = factory(**kwargs)
            step_refs.append(step)
            return step

        monkeypatch.setattr(scope, "begin_public_text_step", begin)
        async with AgentRuntime(store, provider, public_output_protection=scope) as agent:
            thread = await agent.create_thread(str(tmp_path))
            task = asyncio.create_task(
                agent.run_turn(thread.thread_id, "取消测试", request_id="cancel")
            )
            async with asyncio.timeout(5):
                await ready.wait()
                turn_id = (await store.get_thread(thread.thread_id)).active_turn_id
                if case == "parent":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await agent.cancel(thread.thread_id, turn_id)
                    result = await task
                    assert result.status is TurnStatus.CANCELLED
            assert provider.closed and step_refs and step_refs[0]._closed
            assert not step_refs[0]._pending and not step_refs[0]._patterns
            assert CANARY not in (await store.get_thread(thread.thread_id)).model_dump_json()


@pytest.mark.parametrize("case", ["rewrite", "truncate", "overrelease"])
async def test_host_guard_cannot_rewrite_or_drop_original_text(tmp_path, monkeypatch, case):
    with protected() as scope:
        factory = scope.begin_public_text_step

        def begin(**kwargs):
            step = factory(**kwargs)
            original = step.finish

            def finish(content_id, *, checkpoint):
                tail = original(content_id, checkpoint=checkpoint)
                return (
                    "changed"
                    if case == "rewrite"
                    else tail[:-1]
                    if case == "truncate"
                    else tail + "extra"
                )

            step.finish = finish
            return step

        monkeypatch.setattr(scope, "begin_public_text_step", begin)
        store = SQLiteSessionStore(tmp_path / "s.db")
        async with AgentRuntime(
            store, ScriptedProvider([answer("safe")]), public_output_protection=scope
        ) as agent:
            thread = await agent.create_thread(str(tmp_path))
            result = await agent.run_turn(thread.thread_id, "正文一致性", request_id="contract")
            assert result.error.code == "public_output_protection_failed"
            assert all(
                not getattr(item.content, "text", "")
                for item in result.items
                if item.content.kind == "assistant_message"
            )
