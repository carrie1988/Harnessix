"""实际SQLite Runtime、模型历史、SDK回放、非空OTel验证正式Secret值拒绝及合法反馈。"""

from __future__ import annotations

import pytest

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step, approval
from tests.trusted_actions.test_secret_publication import BINDING, CANARY, secret_gateway, source


@pytest.mark.parametrize("case", ["missing", "leak", "safe"])
async def test_formal_bound_secret_at_actual_runtime_surfaces(tmp_path, case):
    scope = SecretPublicationScope((BINDING,), source()) if case != "missing" else None
    body = {"summary": "completed" if case == "safe" else CANARY}
    gate, actions, plans, audit, executor = secret_gateway(
        tmp_path / "workspace", body, scope=scope
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("处理结束")])
    observer, exporter, reader = instrumented()
    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gate, observability=observer
        ) as agent:
            thread = await agent.create_thread(str(tmp_path / "workspace"))
            turn = await agent.run_turn(thread.thread_id, "验证公开值", request_id=case)
            request = approval(turn)
            await agent.reply_approval(
                thread.thread_id,
                turn.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="test"),
            )
            turn = await agent.resume_turn(thread.thread_id, turn.turn_id)
            assert turn.status is (TurnStatus.COMPLETED if case == "safe" else TurnStatus.FAILED)
            results = [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]
            assert results and results[0].trusted_action.state == "succeeded"
            assert results[0].output == (body if case == "safe" else None)
            assert len(provider.requests) == (2 if case == "safe" else 1)
            assert await agent.resume_turn(thread.thread_id, turn.turn_id) == turn
            persisted = await store.get_thread(thread.thread_id)
            events = await store.events(thread.thread_id)
            client = AgentClient(
                InProcessAgentTransport(
                    AgentProtocolServer(
                        AgentApplicationService(
                            agent, store, SQLiteProtocolRequestStore(store.path)
                        )
                    )
                )
            )
            try:
                await client.initialize()
                snapshot = await client.get_thread(thread.thread_id)
                replay = await client.replay_events(thread.thread_id, limit=100)
                assert replay.scanned_through == snapshot.cursor and replay.events
            finally:
                await client.close()
            spans = exporter.get_finished_spans()
            metrics = reader.get_metrics_data()
            assert spans and metrics is not None and metrics.resource_metrics
            public = [
                persisted.model_dump_json(),
                snapshot.model_dump_json(),
                replay.model_dump_json(),
                *(e.model_dump_json() for e in events),
                *(r.model_dump_json() for r in provider.requests),
                *(e.model_dump_json() for e in actions.events(request.plan_id)),
                *(s.to_json() for s in spans),
                metrics.to_json(),
            ]
            assert all(CANARY not in item for item in public)
            assert executor.calls == 1 and executor.reconciliations == 0
        assert all(CANARY.encode() not in p.read_bytes() for p in tmp_path.rglob("*.db*"))
    finally:
        observer.close()
        plans.close()
        audit.close()
        if scope:
            scope.close()
