"""原版本用户输入必须先于Session、Trace和审批副作用被检查。"""

from __future__ import annotations

import asyncio
import base64

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.input_publication import protect_input
from harnessix.agent.models import TextContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, TraceContext
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.agent.test_approvals import approval
from tests.agent.test_interactions import question, question_step
from tests.agent.test_publication import CANARY, protected
from tests.agent.test_telemetry import instrumented


@pytest.mark.parametrize("entry", ["run", "accept"])
@pytest.mark.parametrize("field", ["prompt", "request_id", "trace"])
@pytest.mark.parametrize("encoding", ["raw", "base64"])
async def test_acceptance_rejects_before_any_turn_or_provider(tmp_path, entry, field, encoding):
    value = CANARY if encoding == "raw" else base64.b64encode(CANARY.encode()).decode()
    provider = ScriptedProvider([answer()])
    store = SQLiteSessionStore(tmp_path / "s.db")
    with protected() as scope:
        async with AgentRuntime(store, provider, public_output_protection=scope) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            args = {"request_id": value if field == "request_id" else "safe"}
            if field == "trace":
                args["trace_context"] = TraceContext(
                    traceparent="00-11111111111111111111111111111111-2222222222222222-01",
                    tracestate="fixture=" + value,
                )
            operation = runtime.run_turn if entry == "run" else runtime.accept_turn
            with pytest.raises(KernelError) as caught:
                await operation(thread.thread_id, value if field == "prompt" else "safe", **args)
            assert caught.value.code == "public_input_secret_leak"
            assert CANARY not in str(caught.value)
            assert await store.get_thread(thread.thread_id) == thread
            assert not provider.requests and not runtime._active
    assert not any(CANARY.encode() in p.read_bytes() for p in tmp_path.glob("s.db*"))


async def test_trace_is_checked_before_observer_span(tmp_path):
    observer, exporter, _ = instrumented()
    try:
        with protected() as scope:
            async with AgentRuntime(
                SQLiteSessionStore(tmp_path / "s.db"),
                ScriptedProvider([]),
                public_output_protection=scope,
                observability=observer,
            ) as runtime:
                thread = await runtime.create_thread(str(tmp_path))
                with pytest.raises(KernelError):
                    await runtime.run_turn(
                        thread.thread_id,
                        "safe",
                        request_id="safe",
                        trace_context=TraceContext(
                            traceparent="00-11111111111111111111111111111111-2222222222222222-01",
                            tracestate="fixture=" + CANARY,
                        ),
                    )
        assert not exporter.get_finished_spans()
    finally:
        observer.close()


@pytest.mark.parametrize("entry", ["retry", "accept_retry"])
@pytest.mark.parametrize("field", ["request_id", "trace"])
async def test_retry_input_is_checked_before_new_turn(tmp_path, entry, field):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            accepted = await runtime.accept_turn(thread.thread_id, "safe", request_id="initial")
            await runtime.cancel(thread.thread_id, accepted.turn_id)
            before = await runtime.store.get_thread(thread.thread_id)
            args = {"request_id": CANARY if field == "request_id" else "retry"}
            if field == "trace":
                args["trace_context"] = TraceContext(
                    traceparent="00-11111111111111111111111111111111-2222222222222222-01",
                    tracestate="fixture=" + CANARY,
                )
            operation = runtime.retry_turn if entry == "retry" else runtime.accept_retry_turn
            with pytest.raises(KernelError) as caught:
                await operation(thread.thread_id, accepted.turn_id, **args)
            assert caught.value.code == "public_input_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == before
            assert not runtime._active


@pytest.mark.parametrize("entry", ["workspace", "archive", "fork"])
async def test_thread_lifecycle_rejects_original_input_before_write(tmp_path, entry):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            with pytest.raises(KernelError) as caught:
                if entry == "workspace":
                    await runtime.create_thread(str(tmp_path / CANARY))
                elif entry == "archive":
                    await runtime.archive_thread(thread.thread_id, reason=CANARY)
                else:
                    await runtime.fork_thread(thread.thread_id, request_id=CANARY)
            assert caught.value.code == "public_input_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == thread
            assert (
                len((await runtime.store.list_thread_page(after=None, archived=None, limit=200))[0])
                == 1
            )


async def test_fork_checks_current_registered_snapshot_without_claiming_historical_authorization(
    tmp_path,
):
    store = SQLiteSessionStore(tmp_path / "s.db")
    async with AgentRuntime(store, ScriptedProvider([answer(CANARY)])) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "safe", request_id="legacy")
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(store.path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            before = await runtime.store.get_thread(thread.thread_id)
            with pytest.raises(KernelError) as caught:
                await runtime.fork_thread(thread.thread_id, request_id="safe-fork")
            assert caught.value.code == "public_output_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == before
            assert (
                len((await runtime.store.list_thread_page(after=None, archived=None, limit=200))[0])
                == 1
            )


@pytest.mark.parametrize("field", ["text", "request_id"])
async def test_steering_is_rejected_without_mutating_waiting_turn(tmp_path, field):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            turn = await runtime.accept_turn(thread.thread_id, "safe", request_id="initial")
            before = await runtime.store.get_thread(thread.thread_id)
            with pytest.raises(KernelError) as caught:
                await runtime.steer_turn(
                    thread.thread_id,
                    turn.turn_id,
                    CANARY if field == "text" else "safe",
                    request_id=CANARY if field == "request_id" else "s",
                )
            assert caught.value.code == "public_input_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == before
            safe = await runtime.steer_turn(
                thread.thread_id, turn.turn_id, "中文追加", request_id="s"
            )
            assert (
                await runtime.steer_turn(thread.thread_id, turn.turn_id, "中文追加", request_id="s")
                == safe
            )
            assert any(
                isinstance(i.content, TextContent) and i.content.text == "中文追加"
                for i in safe.items
            )


async def test_question_rejection_preserves_wait_and_atomic_safe_answer(tmp_path):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([question_step(), answer()]),
            public_output_protection=scope,
            enable_questions=True,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            waiting = await runtime.run_turn(thread.thread_id, "safe", request_id="q")
            request = question(waiting)
            before = await runtime.store.get_thread(thread.thread_id)
            with pytest.raises(KernelError) as caught:
                await runtime.reply_question(
                    thread.thread_id, waiting.turn_id, request.question_id, answer=CANARY
                )
            assert caught.value.code == "public_input_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == before
            replied = await runtime.reply_question(
                thread.thread_id, waiting.turn_id, request.question_id, answer="生产"
            )
            assert replied.status == TurnStatus.EXECUTING_TOOLS
            assert (
                await runtime.store.get_thread(thread.thread_id)
            ).sequence == before.sequence + 5
            assert (
                await runtime.reply_question(
                    thread.thread_id, waiting.turn_id, request.question_id, answer="生产"
                )
                == replied
            )
            assert (
                await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            ).status == TurnStatus.COMPLETED


@pytest.mark.parametrize("field", ["actor", "reason", "fingerprint"])
async def test_approval_rejection_precedes_decision_and_execution(tmp_path, field):
    tools = RecordingTools(approval=True)
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([tool_step("test.read"), answer()]),
            tools,
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            waiting = await runtime.run_turn(thread.thread_id, "safe", request_id="a")
            request = approval(waiting)
            before = await runtime.store.get_thread(thread.thread_id)
            decision = ApprovalDecision(
                outcome=ApprovalOutcome.APPROVED,
                actor=CANARY if field == "actor" else "reviewer",
                reason=CANARY if field == "reason" else None,
            )
            with pytest.raises(KernelError) as caught:
                await runtime.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    request.approval_id,
                    fingerprint=CANARY if field == "fingerprint" else request.request_fingerprint,
                    decision=decision,
                )
            assert caught.value.code == "public_input_secret_leak"
            assert await runtime.store.get_thread(thread.thread_id) == before
            assert not tools.calls
            await runtime.reply_approval(
                thread.thread_id,
                waiting.turn_id,
                request.approval_id,
                fingerprint=request.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
            )
            assert (
                await runtime.resume_turn(thread.thread_id, waiting.turn_id)
            ).status == TurnStatus.COMPLETED
            assert len(tools.calls) == 1


@pytest.mark.parametrize("mode", ["closed", "limit", "extension", "timeout", "parent"])
async def test_input_failure_and_cancel_never_commit_partial_acceptance(
    tmp_path, monkeypatch, mode
):
    from harnessix.agent import publication as boundary
    from harnessix.secrets import publication

    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            if mode == "closed":
                scope.close()
            elif mode == "limit":
                monkeypatch.setattr(publication, "MAX_SCAN_WORK", 2)
            clock = [boundary.monotonic()]
            monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])
            original = publication._ScanBudget.step

            def step(work, depth):
                if mode == "extension":
                    raise RuntimeError(CANARY)
                if mode == "timeout":
                    clock[0] += 20
                if mode == "parent":
                    asyncio.current_task().cancel()
                original(work, depth)

            monkeypatch.setattr(publication._ScanBudget, "step", step)
            with pytest.raises(
                asyncio.CancelledError if mode == "parent" else KernelError
            ) as caught:
                await asyncio.create_task(
                    runtime.accept_turn(thread.thread_id, "safe", request_id="r")
                )
            if mode != "parent":
                assert (
                    caught.value.code
                    == {
                        "closed": "public_input_secret_unavailable",
                        "limit": "public_input_limit",
                        "extension": "public_input_protection_failed",
                        "timeout": "public_input_timeout",
                    }[mode]
                )
                assert CANARY not in str(caught.value)
            assert await runtime.store.get_thread(thread.thread_id) == thread


async def test_input_token_cancellation_is_not_translated_to_failure():
    token = CancelToken()
    token.cancel()
    with protected() as scope, pytest.raises(TurnCancelled):
        await protect_input(scope, {"text": "safe"}, token)


async def test_safe_inputs_keep_original_fingerprint_idempotence_and_restart(tmp_path):
    path = tmp_path / "s.db"
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(path), ScriptedProvider([]), public_output_protection=scope
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            accepted = await runtime.accept_turn(
                thread.thread_id, "中文原始输入", request_id="same"
            )
            before = await runtime.store.get_thread(thread.thread_id)
            assert (
                await runtime.accept_turn(thread.thread_id, "中文原始输入", request_id="same")
                == accepted
            )
            assert await runtime.store.get_thread(thread.thread_id) == before
            with pytest.raises(KernelError) as caught:
                await runtime.accept_turn(thread.thread_id, "不同内容", request_id="same")
            assert caught.value.code == "request_conflict"
        async with AgentRuntime(
            SQLiteSessionStore(path), ScriptedProvider([answer()]), public_output_protection=scope
        ) as runtime:
            assert (
                await runtime.accept_turn(thread.thread_id, "中文原始输入", request_id="same")
                == accepted
            )
            assert (
                await runtime.resume_turn(thread.thread_id, accepted.turn_id)
            ).status == TurnStatus.COMPLETED


@pytest.mark.parametrize("field", ["actor", "reason"])
async def test_trusted_approval_rejection_precedes_owner_and_action_audit(tmp_path, field):
    from harnessix.trusted_actions.contracts import ActionExecutionOutcome
    from tests.agent.test_trusted_action_runtime import action_step
    from tests.agent.test_trusted_action_runtime import approval as action_approval
    from tests.trusted_actions.test_agent_gateway import FakeExecutor, build_gateway

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("before")
    executor = FakeExecutor(ActionExecutionOutcome(kind="succeeded", output={"summary": "changed"}))
    gateway, router, plans, audit = build_gateway(root, executor)
    try:
        with protected() as scope:
            async with AgentRuntime(
                SQLiteSessionStore(tmp_path / "s.db"),
                ScriptedProvider([action_step(), answer()]),
                trusted_actions=gateway,
                public_output_protection=scope,
            ) as runtime:
                thread = await runtime.create_thread(str(root))
                waiting = await runtime.run_turn(thread.thread_id, "safe", request_id="action")
                request = action_approval(waiting)
                before = await runtime.store.get_thread(thread.thread_id)
                audit_before = audit.events(request.plan_id)
                with pytest.raises(KernelError) as caught:
                    await runtime.reply_approval(
                        thread.thread_id,
                        waiting.turn_id,
                        request.approval_id,
                        fingerprint=request.request_fingerprint,
                        decision=ApprovalDecision(
                            outcome=ApprovalOutcome.APPROVED,
                            actor=CANARY if field == "actor" else "reviewer",
                            reason=CANARY if field == "reason" else None,
                        ),
                    )
                assert caught.value.code == "public_input_secret_leak"
                assert await runtime.store.get_thread(thread.thread_id) == before
                assert audit.events(request.plan_id) == audit_before
                assert router.status(request.plan_id).state == "pending_approval"
                assert executor.calls == 0 and (root / "file.txt").read_text() == "before"
                await runtime.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    request.approval_id,
                    fingerprint=request.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
                )
                assert (
                    await runtime.resume_turn(thread.thread_id, waiting.turn_id)
                ).status == TurnStatus.COMPLETED
                assert executor.calls == 1
    finally:
        plans.close()
        audit.close()


@pytest.mark.parametrize(
    "code, category",
    [
        ("public_input_secret_leak", "input"),
        ("public_input_limit", "budget"),
        ("public_input_timeout", "budget"),
        ("public_input_secret_unavailable", "internal"),
        ("public_input_protection_failed", "internal"),
    ],
)
def test_input_failure_categories_remain_stable(code, category):
    from harnessix.agent.errors import AgentFailure

    assert AgentFailure(code=code, message="输入未通过公开数据保护校验").category.value == category


async def test_utf8_byte_limit_rejects_before_acceptance_even_with_valid_character_limit(tmp_path):
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(tmp_path))
            with pytest.raises(KernelError) as caught:
                await runtime.accept_turn(thread.thread_id, "界" * 400000, request_id="r")
            assert caught.value.code == "public_input_limit"
            assert await runtime.store.get_thread(thread.thread_id) == thread
