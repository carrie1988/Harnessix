"""实际产品Profile经正式SDK修正参数、独立审批与持久回放；网络Provider为离线替身。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from harnessix.agent.models import ToolResultContent
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.protocol.contracts import (
    ApprovalRespondParams,
    PublicApprovalDecision,
    PublicApprovalRequestContent,
    PublicToolResultContent,
)
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.agent.test_publication import protected
from tests.product_config.test_process_action import (
    _fake_engine,
    _NoSecrets,
    _probe_runner,
    _profile,
    _TerminalRuntime,
)
from tests.product_config.test_product_rollback_sdk import contents, wait_turn
from tests.trusted_actions.test_plan_error_boundaries import _assert_preflight_stores


def step(index: int, arguments: dict) -> list:
    return [
        ResponseStarted(response_id=f"profile-{index}"),
        ToolCallCompleted(
            call_id=f"profile-call-{index}", tool="run_profile.unit-tests", arguments=arguments
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


@pytest.mark.skipif(os.name != "posix", reason="固定Container Profile装配使用POSIX Owner")
@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_sdk_eight_invalid_calls_then_correct_profile_require_new_approval(
    tmp_path: Path, cancel_waiting: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    profile = _profile(_fake_engine(tmp_path))
    secrets = _NoSecrets()
    async with PosixProcessSupervisor(tmp_path / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        )
        assert probe.verified is not None
        process = _TerminalRuntime(probe.verified.runtime.capability)
        verified = replace(probe.verified, runtime=cast(Any, process))
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    environment = build_fixed_product_action_environment(root)
    router = TrustedActionRouter(
        plans=plans, audit=audit, workspace_root=environment.workspace_root
    )
    # 先停在安全文本响应，核验八次拒绝没有Route或进程，再发独立修正调用。
    provider = ScriptedProvider(
        [step(i, {"selectors": []} if i == 0 else {}) for i in range(8)] + [answer("参数无效")]
    )
    try:
        with protected() as protection:
            artifacts = SQLiteArtifactStore(sessions, public_output_protection=protection)
            async with CodingToolRuntime(root, artifacts=artifacts) as tools:
                composition = build_product_action_composition(
                    build_product_action_config(
                        workspace_patch_enabled=False, process_profiles=(profile,)
                    ),
                    environment,
                    router,
                    transactions,
                    leases,
                    artifacts,
                    artifact_workspace_scope=tools.workspace_scope,
                    process_probes=(
                        ProductProcessProfileProbeResult(
                            profile=profile, reason_code="verified", verified=verified
                        ),
                    ),
                    secrets=secrets,
                )
                assert composition.gateway is not None
                async with AgentRuntime(
                    sessions,
                    provider,
                    scoped_tools=tools,
                    artifacts=artifacts,
                    trusted_actions=composition.gateway,
                    public_output_protection=protection,
                ) as runtime:
                    service = AgentApplicationService(
                        runtime, sessions, SQLiteProtocolRequestStore(sessions.path)
                    )
                    client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
                    try:
                        await client.initialize()
                        thread = await client.create_thread(str(root), request_id="profile-thread")
                        await client.start_turn(
                            thread.thread_id, "执行固定检查", request_id="invalid"
                        )
                        first = await wait_turn(client, thread.thread_id, "completed")
                        failures = await contents(
                            client, thread.thread_id, first.turn_id, PublicToolResultContent
                        )
                        # 协议回放含Item创建与终态两个版本，按call_id取最终事实而非事件数量。
                        failures = list({item.call_id: item for item in failures}.values())
                        assert len(failures) == 8
                        assert all(
                            f.error.code == "tool_invalid_arguments"
                            and "缺少必填字段：profile" in f.error.message
                            for f in failures
                        )
                        assert len(provider.requests) == 9 and process.run_calls == 0
                        for request in provider.requests[1:]:
                            assert any(
                                isinstance(item.content, ToolResultContent)
                                and "缺少必填字段：profile" in item.content.error.message
                                for item in request.history
                                if isinstance(item.content, ToolResultContent)
                                and item.content.error
                            )
                        _assert_preflight_stores(tmp_path)
                        provider.steps = (step(8, {"profile": "unit-tests"}), answer("检查完成"))
                        await client.start_turn(
                            thread.thread_id, "修正参数后执行", request_id="corrected"
                        )
                        waiting = await wait_turn(client, thread.thread_id, "waiting_approval")
                        approval = (
                            await contents(
                                client,
                                thread.thread_id,
                                waiting.turn_id,
                                PublicApprovalRequestContent,
                            )
                        )[-1]
                        assert process.run_calls == 0 and approval.approval_type == "process"
                        if cancel_waiting:
                            await client.cancel_turn(
                                thread.thread_id, waiting.turn_id, request_id="cancel"
                            )
                            terminal = await client.get_thread(thread.thread_id)
                            assert terminal.latest_turn.status == "cancelled"
                            assert process.run_calls == 0
                        else:
                            await client.respond_approval(
                                ApprovalRespondParams(
                                    request_id="approve",
                                    thread_id=thread.thread_id,
                                    turn_id=waiting.turn_id,
                                    approval_id=approval.approval_id,
                                    fingerprint=approval.request_fingerprint,
                                    decision=PublicApprovalDecision(
                                        outcome="approved", actor="reviewer"
                                    ),
                                )
                            )
                            await wait_turn(client, thread.thread_id, "completed")
                            results = await contents(
                                client, thread.thread_id, waiting.turn_id, PublicToolResultContent
                            )
                            results = list({item.call_id: item for item in results}.values())
                            assert len(results) == 1 and results[0].outcome == "succeeded"
                            assert process.run_calls == 1 and process.reconcile_calls == 0
                        original = await sessions.get_thread(thread.thread_id)
                        assert replay(await sessions.events(thread.thread_id)) == original
                    finally:
                        await client.close()
        reopened = SQLiteSessionStore(sessions.path)
        assert await reopened.get_thread(thread.thread_id) == original
        assert replay(await reopened.events(thread.thread_id)) == original
    finally:
        plans.close()
        audit.close()
        transactions.close()
        leases.close()
