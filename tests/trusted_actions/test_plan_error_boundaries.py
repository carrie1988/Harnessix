"""计划回调错误经真实Runtime、Session、协议及遥测的端到端泄漏回归。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import BaseModel

from harnessix.agent.approvals import tool_fingerprint
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import pending_calls, replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.domain.models import EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.scripted import ScriptedProvider
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk.agent_client import AgentClient, InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
from harnessix.trusted_actions.contracts import ActionExecutionOutcome, build_trusted_tool_binding
from harnessix.trusted_actions.policy import DefaultCodingRiskPolicy
from harnessix.trusted_actions.public_errors import (
    _DECODER_ERRORS,
    _RESOLVER_ERRORS,
    sanitize_plan_exception,
)
from harnessix.trusted_actions.router import (
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.agent.helpers import answer
from tests.agent.test_telemetry import instrumented
from tests.agent.test_trusted_action_runtime import action_step
from tests.trusted_actions.test_agent_gateway import (
    FakeExecutor,
    FileInput,
    descriptor,
    runtime_context,
)
from tests.trusted_actions.test_public_error_leakage import _assert_no_leak, _payload, _store_bytes
from tests.trusted_actions.test_router import context, invocation


def _composition(root: Path, stage: str, error: BaseException):
    """只装配一个真实只读Binding；故障发生在副作用和Route落盘之前。"""

    tool = descriptor().model_copy(
        update={
            "effect_class": EffectClass.READ_ONLY,
            "risk_level": RiskLevel.LOW,
            "requires_idempotency": False,
            "requires_approval": False,
            "supports_reconciliation": False,
        }
    )
    binding = build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=tool.name,
        tool_version=tool.version,
        tool_fingerprint=tool_fingerprint(tool),
        input_schema_sha256=canonical_digest(tool.input_schema),
        effect_class=tool.effect_class,
        risk_level=tool.risk_level,
        recovery_mode="none",
        executor_id="test.plan-errors",
    )
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded"))

    def reject(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    class BrokenPolicy(DefaultCodingRiskPolicy):
        def evaluate(self, *_args: object, **_kwargs: object) -> NoReturn:
            reject()

    def resolve(arguments: BaseModel, _context: object) -> ResolvedAction:
        FileInput.model_validate(arguments)
        if stage == "resolve":
            reject()
        return ResolvedAction(resources=())

    plans = SQLiteExecutionPlanStore(root.parent / "plans.db")
    audit = SQLiteActionAuditStore(root.parent / "audit.db")
    actions = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=lambda _: root,
        policy=BrokenPolicy() if stage == "policy" else None,
    )
    actions.register(
        TrustedActionDefinition(
            binding,
            FileInput,
            resolve,
            executor,
            input_schema=tool.input_schema if stage == "decode" else None,
            decode_arguments=reject if stage == "decode" else None,
        )
    )
    gateway = RouterBackedAgentActionGateway(actions, (tool,), lambda *_: runtime_context(root))
    return gateway, actions, plans, audit, executor, binding


def _assert_preflight_stores(root: Path) -> None:
    tables_by_file = {
        "plans.db": ("execution_plans", "execution_approvals"),
        "audit.db": ("action_route_plans", "action_route_snapshots", "action_audit_events"),
    }
    for filename, tables in tables_by_file.items():
        with closing(sqlite3.connect(root / filename)) as connection:
            for table in tables:
                assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
    _assert_no_leak(*_store_bytes(*root.glob("*.db*")))


@pytest.mark.parametrize("stage", ["decode", "resolve", "policy"])
@pytest.mark.parametrize(
    "code", ["secret_in_code_canary", "action_resource_invalid", "tool_invalid_arguments"]
)
async def test_plan_callback_errors_never_leak_across_public_surfaces(
    tmp_path: Path, stage: str, code: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    error = KernelError(code, _payload(), retryable=True)
    gateway, actions, plans, audit, executor, _binding = _composition(root, stage, error)
    store = SQLiteSessionStore(tmp_path / "session.db")
    provider = ScriptedProvider([action_step(), answer("已处理参数拒绝")])
    observer, exporter, reader = instrumented()
    recoverable = (stage, code) in {
        ("decode", "tool_invalid_arguments"),
        ("resolve", "action_resource_invalid"),
    }
    try:
        async with AgentRuntime(
            store, provider, trusted_actions=gateway, observability=observer
        ) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "验证计划拒绝", request_id="plan-error")
            assert turn.status is (TurnStatus.COMPLETED if recoverable else TurnStatus.FAILED)
            assert not pending_calls(turn)
            results = [
                item.content for item in turn.items if isinstance(item.content, ToolResultContent)
            ]
            assert len(results) == 1
            failure = results[0].error
            assert failure is not None
            assert failure.code == (code if recoverable else "action_plan_failed")
            assert not failure.retryable
            if not recoverable:
                assert turn.error == failure
            assert executor.calls == 0 and executor.reconciliations == 0
            assert len(provider.requests) == (2 if recoverable else 1)
            # 可恢复参数拒绝确实送入下一次模型历史，不只是检查本地对象。
            if recoverable:
                assert any(
                    isinstance(item.content, ToolResultContent)
                    for item in provider.requests[-1].history
                )
            assert await runtime.resume_turn(thread.thread_id, turn.turn_id) == turn
            events = await store.events(thread.thread_id)
            assert replay(events) == await store.get_thread(thread.thread_id)

            service = AgentApplicationService(
                runtime, store, SQLiteProtocolRequestStore(store.path)
            )
            client = AgentClient(InProcessAgentTransport(AgentProtocolServer(service)))
            try:
                await client.initialize()
                snapshot = await client.get_thread(thread.thread_id)
                protocol_events = await client.replay_events(thread.thread_id, limit=100)
                assert snapshot.latest_turn is not None
                assert snapshot.latest_turn.status == turn.status.value
                assert protocol_events.scanned_through == snapshot.cursor
                _assert_no_leak(snapshot.model_dump_json(), protocol_events.model_dump_json())
            finally:
                await client.close()

            spans = exporter.get_finished_spans()
            assert any(span.name == "harnessix.agent.turn" for span in spans)
            metric_data = reader.get_metrics_data()
            assert metric_data is not None and metric_data.resource_metrics
            _assert_no_leak(
                turn.model_dump_json(),
                *(event.model_dump_json() for event in events),
                *(request.model_dump_json() for request in provider.requests),
                *(span.to_json() for span in spans),
                metric_data.to_json(),
            )
        # 计划回调失败不得生成半个执行计划、Audit事件或批准检查点。
        _assert_preflight_stores(tmp_path)
    finally:
        observer.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize(
    "stage,registry", [("decode", _DECODER_ERRORS), ("resolve", _RESOLVER_ERRORS)]
)
def test_every_registered_error_uses_only_fixed_message(
    stage: str, registry: dict[str, str]
) -> None:
    for code, message in registry.items():
        original = KernelError(code, _payload(), retryable=True)
        sanitized = sanitize_plan_exception(original, stage=stage)  # type: ignore[arg-type]
        assert sanitized is not original
        assert sanitized.code == code and sanitized.message == message
        assert not sanitized.retryable
        _assert_no_leak(str(sanitized))


@pytest.mark.parametrize("stage", ["decode", "resolve", "policy"])
def test_callback_cancellation_is_not_converted_to_a_public_failure(
    tmp_path: Path, stage: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    gateway, actions, plans, audit, executor, binding = _composition(
        root, stage, asyncio.CancelledError()
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            actions.plan(invocation(binding), context(root))
        assert executor.calls == 0 and executor.reconciliations == 0
    finally:
        gateway.close()
        plans.close()
        audit.close()


@pytest.mark.parametrize("stage", ["decode", "resolve", "policy"])
def test_callback_timeout_is_fixed_without_retry_or_persisted_effect(
    tmp_path: Path, stage: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    gateway, actions, plans, audit, executor, binding = _composition(
        root, stage, TimeoutError(_payload())
    )
    try:
        with pytest.raises(KernelError) as caught:
            actions.plan(invocation(binding), context(root))
        assert caught.value.code == "action_plan_failed"
        assert not caught.value.retryable
        assert caught.value.__suppress_context__
        _assert_no_leak(str(caught.value))
        assert executor.calls == 0 and executor.reconciliations == 0
        _assert_preflight_stores(tmp_path)
    finally:
        gateway.close()
        plans.close()
        audit.close()


def test_sanitizer_does_not_render_an_untrusted_exception() -> None:
    class UnrenderableError(KernelError):
        def __str__(self) -> NoReturn:
            raise AssertionError("不得渲染原异常")

        def __repr__(self) -> NoReturn:
            raise AssertionError("不得渲染原异常")

    sanitized = sanitize_plan_exception(
        UnrenderableError("workspace_path_denied", _payload(), retryable=True), stage="resolve"
    )
    assert sanitized.code == "workspace_path_denied"
    assert not sanitized.retryable
    _assert_no_leak(str(sanitized), repr(sanitized))
