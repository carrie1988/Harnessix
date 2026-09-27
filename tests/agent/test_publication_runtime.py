"""真实SQLite、只读工具、Artifact、模型历史与SDK验证公开保护，不发起外部模型请求。"""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID, uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.artifacts import ScopedProtocolArtifactReader
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, AgentSDKError, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.agent.test_publication import CANARY, protected
from tests.agent.test_telemetry import instrumented
from tests.artifacts.helpers import results, step


@pytest.mark.parametrize("tool", ["read_file", "grep"])
@pytest.mark.parametrize("case", ["safe", "leak"])
async def test_read_only_result_and_complete_artifact_before_publication(tmp_path, tool, case):
    root = tmp_path / "repo"
    root.mkdir()
    lines = ["needle benign\n"] * 300
    if case == "leak":
        lines[149] = "needle " + CANARY + "\n"
    (root / "main.py").write_text("".join(lines))
    args = (
        {"path": "main.py", "start_line": 1, "max_lines": 200}
        if tool == "read_file"
        else {"query": "needle", "max_results": 2}
    )
    provider = ScriptedProvider([step(tool, **args), answer("读取完成")])
    store = SQLiteSessionStore(tmp_path / "s.db")
    observer, exporter, reader = instrumented()
    with protected() as scope:
        artifacts = SQLiteArtifactStore(store, public_output_protection=scope)
        try:
            async with CodingToolRuntime(root, artifacts=artifacts) as tools:
                async with AgentRuntime(
                    store,
                    provider,
                    scoped_tools=tools,
                    artifacts=artifacts,
                    public_output_protection=scope,
                    observability=observer,
                ) as agent:
                    thread = await agent.create_thread(str(tools.workspace_root))
                    turn = await agent.run_turn(thread.thread_id, "读取工作区", request_id="read")
                    assert turn.status is (
                        TurnStatus.COMPLETED if case == "safe" else TurnStatus.FAILED
                    )
                    if case == "leak":
                        assert turn.error.code == "public_output_secret_leak"
                    assert len(provider.requests) == (2 if case == "safe" else 1)
                    assert await agent.resume_turn(thread.thread_id, turn.turn_id) == turn
                    persisted = await store.get_thread(thread.thread_id)
                    client = AgentClient(
                        InProcessAgentTransport(
                            AgentProtocolServer(
                                AgentApplicationService(
                                    agent,
                                    store,
                                    SQLiteProtocolRequestStore(store.path),
                                    ScopedProtocolArtifactReader(store, artifacts, tools),
                                )
                            )
                        )
                    )
                    await client.initialize()
                    try:
                        snapshot = await client.get_thread(thread.thread_id)
                        replay = await client.replay_events(thread.thread_id, limit=200)
                        assert replay.events
                        if tool == "grep" and case == "safe":
                            ref = results(turn)[0].output["artifact"]
                            page = await client.read_artifact(
                                thread.thread_id, UUID(ref["artifact_id"]), offset=149, limit=1
                            )
                            assert page.text and page.artifact.records == 300
                        else:
                            page = None
                    finally:
                        await client.close()
                    spans = exporter.get_finished_spans()
                    metrics = reader.get_metrics_data()
                    assert spans and metrics is not None and metrics.resource_metrics
                    public = [
                        persisted.model_dump_json(),
                        snapshot.model_dump_json(),
                        replay.model_dump_json(),
                        *(r.model_dump_json() for r in provider.requests),
                        *(s.to_json() for s in spans),
                        metrics.to_json(),
                    ]
                    if page:
                        public.append(page.model_dump_json())
                    assert all(CANARY not in p for p in public)
            assert all(CANARY.encode() not in p.read_bytes() for p in tmp_path.glob("*.db*"))
            with sqlite3.connect(store.path) as db:
                rows = db.execute(
                    "SELECT publication_epoch,publication_policy FROM agent_artifacts"
                ).fetchall()
            if tool == "grep" and case == "safe":
                assert len(rows) == 1 and rows[0][0] == artifacts._publication.epoch
            else:
                assert not rows
        finally:
            observer.close()


@pytest.mark.parametrize("case", ["legacy", "reopen", "tamper"])
async def test_sdk_and_tool_cannot_authorize_historical_artifact_with_current_key(tmp_path, case):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle benign\n" * 300)
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        producer = SQLiteArtifactStore(
            store, public_output_protection=None if case == "legacy" else scope
        )
        async with CodingToolRuntime(root, artifacts=producer) as tools:
            async with AgentRuntime(
                store, ScriptedProvider([step(), answer()]), scoped_tools=tools, artifacts=producer
            ) as agent:
                thread = await agent.create_thread(str(tools.workspace_root))
                turn = await agent.run_turn(thread.thread_id, "发布归档", request_id="original")
                assert turn.status is TurnStatus.COMPLETED
                ref = results(turn)[0].output["artifact"]
                original_call = next(
                    i.content for i in turn.items if isinstance(i.content, ToolCallContent)
                )
        if case == "tamper":
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE agent_artifacts SET publication_epoch = ?", (str(uuid4()),))
            consumer = producer
        else:
            consumer = SQLiteArtifactStore(store, public_output_protection=scope)
        provider = ScriptedProvider(
            [step("read_artifact", artifact_id=ref["artifact_id"], offset=149, limit=1), answer()]
        )
        async with CodingToolRuntime(root, artifacts=consumer) as tools:
            async with AgentRuntime(
                store,
                provider,
                scoped_tools=tools,
                artifacts=consumer,
                public_output_protection=scope,
            ) as agent:
                client = AgentClient(
                    InProcessAgentTransport(
                        AgentProtocolServer(
                            AgentApplicationService(
                                agent,
                                store,
                                SQLiteProtocolRequestStore(store.path),
                                ScopedProtocolArtifactReader(store, consumer, tools),
                            )
                        )
                    )
                )
                await client.initialize()
                try:
                    with pytest.raises(AgentSDKError) as caught:
                        await client.read_artifact(
                            thread.thread_id, UUID(ref["artifact_id"]), offset=149, limit=1
                        )
                    assert caught.value.code == "artifact_publication_unproven"
                    with pytest.raises(KernelError) as failed:
                        await consumer.verify_reference(
                            thread.thread_id,
                            original_call.call_id,
                            ArtifactRef.model_validate_json(json.dumps(ref)),
                            workspace_scope=tools.workspace_scope,
                            purpose="tool_result",
                        )
                    assert failed.value.code == "artifact_publication_unproven"
                    resumed = await agent.run_turn(
                        thread.thread_id, "重开归档", request_id="reopen"
                    )
                    assert resumed.status is TurnStatus.FAILED
                    assert not provider.requests
                finally:
                    await client.close()


async def test_confirmed_write_keeps_audit_and_never_reexecutes_after_global_rejection(tmp_path):
    from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
    from harnessix.trusted_actions.contracts import ActionExecutionOutcome
    from tests.agent.test_trusted_action_runtime import action_step, approval
    from tests.trusted_actions.test_agent_gateway import FakeExecutor, build_gateway

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before")

    class WritingExecutor(FakeExecutor):
        async def execute(self, plan, arguments):
            (root / "file.txt").write_text("changed")
            return await super().execute(plan, arguments)

    executor = WritingExecutor(ActionExecutionOutcome(kind="succeeded", output={"summary": CANARY}))
    gateway, router, plans, audit = build_gateway(root, executor)
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer()])
    try:
        with protected() as scope:
            async with AgentRuntime(
                store, provider, trusted_actions=gateway, public_output_protection=scope
            ) as agent:
                thread = await agent.create_thread(str(root))
                waiting = await agent.run_turn(thread.thread_id, "验证效果", request_id="write")
                request = approval(waiting)
                await agent.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="test"),
                )
                terminal = await agent.resume_turn(thread.thread_id, waiting.turn_id)
                assert terminal.status is TurnStatus.FAILED
                assert terminal.error.code == "public_output_secret_leak"
                recorded = results(terminal)
                assert len(recorded) == 1 and recorded[0].output is None
                assert recorded[0].trusted_action.state == "succeeded"
                frozen = router.status(request.plan_id)
                assert frozen.state == "succeeded"
                audit_before = [e.model_dump_json() for e in router.events(request.plan_id)]
                assert await agent.resume_turn(thread.thread_id, terminal.turn_id) == terminal
                assert router.status(request.plan_id) == frozen
                assert [e.model_dump_json() for e in router.events(request.plan_id)] == audit_before
                assert executor.calls == 1 and executor.reconciliations == 0
                assert (root / "file.txt").read_text() == "changed"
                assert len(provider.requests) == 1
                assert CANARY not in (await store.get_thread(thread.thread_id)).model_dump_json()
                assert all(
                    CANARY not in e.model_dump_json() for e in await store.events(thread.thread_id)
                )
                assert all(CANARY not in s for s in audit_before)
    finally:
        plans.close()
        audit.close()
