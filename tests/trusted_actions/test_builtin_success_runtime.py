"""无Owner的正式成功摘要经实际Runtime、Session、Model、Protocol和遥测失败关闭。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    EffectClass,
    RiskLevel,
    ToolDescriptor,
)
from harnessix.execution.contracts import canonical_digest
from harnessix.mcp.schema import McpToolArguments
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, build_trusted_tool_binding
from harnessix.trusted_actions.router import (
    ResolvedAction,
    TrustedActionDefinition,
    canonical_action_resource,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import approval
from tests.trusted_actions.test_agent_gateway import runtime_context
from tests.trusted_actions.test_gateway_error_runtime import (
    assert_runtime_surfaces,
    assert_stores_safe,
)
from tests.trusted_actions.test_public_error_leakage import _payload
from tests.trusted_actions.test_router import router


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("case", ["extra", "profile", "state"])
async def test_invalid_inline_success_never_reaches_public_runtime_surfaces(
    tmp_path, read_only, case
):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("unchanged", encoding="utf-8")
    effect = EffectClass.READ_ONLY if read_only else EffectClass.NON_IDEMPOTENT_WRITE
    descriptor = ToolDescriptor(
        name="run_profile.verify",
        version="1",
        description="验证正式内联摘要",
        input_schema=McpToolArguments.model_json_schema(),
        effect_class=effect,
        risk_level=RiskLevel.LOW if read_only else RiskLevel.HIGH,
        requires_idempotency=not read_only,
        requires_approval=not read_only,
        supports_reconciliation=not read_only,
    )
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=descriptor.name,
        tool_version=descriptor.version,
        tool_fingerprint=tool_fingerprint(descriptor),
        input_schema_sha256=canonical_digest(descriptor.input_schema),
        effect_class=effect,
        risk_level=descriptor.risk_level,
        recovery_mode="none" if read_only else "durable_ledger",
        executor_id="product.process-profile.verify",
    )

    class Returning:
        calls, reconciliations, plan_id = 0, 0, None

        async def execute(self, plan, arguments):
            self.calls += 1
            self.plan_id = plan.execution.plan_id
            stream = {
                "observed_bytes": 0,
                "observed_sha256": hashlib.sha256(b"").hexdigest(),
                "persisted_bytes": 0,
                "truncated": False,
                "eof": True,
            }
            body = {
                "version": "trusted-process-output/v1",
                "profile": "verify",
                "process_id": str(self.plan_id),
                "state": "exited",
                "returncode": 0,
                "stop_reason": "exited",
                "stdout": stream,
                "stderr": dict(stream),
                "complete": True,
            }
            if case == "extra":
                body["diagnostic"] = _payload()
            elif case == "profile":
                body["profile"] = "different"
            else:
                body["state"], body["stop_reason"], body["returncode"] = (
                    "unknown",
                    "host_lost",
                    None,
                )
            return ActionExecutionOutcome(kind="succeeded", output=body)

        async def reconcile(self, *_args):
            self.reconciliations += 1
            raise AssertionError("已确认成功事实不得重复对账")

    executor = Returning()
    actions, plans, audit = router(root)
    access = "read" if read_only else "write"
    actions.register(
        TrustedActionDefinition(
            binding,
            McpToolArguments,
            lambda *_: ResolvedAction(
                resources=(
                    canonical_action_resource(
                        kind="workspace", access=access, identifier={"path": "file.txt"}
                    ),
                ),
                workspace_resources=(WorkspaceResourceRequest(path="file.txt", access=access),),
            ),
            executor,
        )
    )
    gateway = RouterBackedAgentActionGateway(
        actions, (descriptor,), lambda *_: runtime_context(root)
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="inline-response"),
                ToolCallCompleted(
                    call_id="inline-call", tool=descriptor.name, arguments={"profile": "verify"}
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("处理结束"),
        ]
    )
    observer, exporter, reader = instrumented()
    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(
                thread.thread_id, "验证内联成功结果", request_id="inline-public-contract"
            )
            if not read_only:
                request = approval(turn)
                await runtime.reply_approval(
                    thread.thread_id,
                    turn.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED, actor="inline-contract-test"
                    ),
                )
                turn = await runtime.resume_turn(thread.thread_id, turn.turn_id)
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert len(results) == 1 and results[0].output is None
            assert turn.status is TurnStatus.FAILED
            assert turn.error.code == "trusted_action_output_mismatch"
            assert results[0].outcome == "succeeded"
            assert results[0].trusted_action.state == "succeeded"
            assert len(provider.requests) == 1
            assert actions.status(executor.plan_id).state == "succeeded"
            assert actions.events(executor.plan_id)[-1].error_code is None
            assert all(operation.state == "completed" for operation in audit.operations())
            assert executor.calls == 1 and executor.reconciliations == 0
            await assert_runtime_surfaces(runtime, store, thread, turn, provider, exporter, reader)
        assert_stores_safe(tmp_path)
        assert (root / "file.txt").read_text(encoding="utf-8") == "unchanged"
    finally:
        observer.close()
        plans.close()
        audit.close()
