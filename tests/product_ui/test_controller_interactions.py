from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.approval_automation import ApprovalAutomation
from harnessix.product_ui.controller import (
    CreateThreadIntent,
    ProductController,
    StartRequest,
    SubmitPromptIntent,
    ToggleAutoAcceptIntent,
)
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interactions import (
    ApprovalBinding,
    ApprovalEvidenceStatus,
    CancelTurnIntent,
    LoadApprovalEvidenceIntent,
    PendingApproval,
    RespondApprovalIntent,
    RespondQuestionIntent,
    SteerTurnIntent,
    active_turn_control,
    pending_approval,
    pending_question,
)
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


async def _wait_for_turn(controller: ProductController, status: str):
    async with asyncio.timeout(10):
        while True:
            state = controller.state
            turn = state.thread_view.current_turn if state.thread_view is not None else None
            if turn is not None and turn.status == status:
                return state
            await asyncio.sleep(0.02)


async def test_controller_approval_uses_evidence_and_one_prepared_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer("审批后完成")])
    async with AgentRuntime(sessions, provider, tools) as runtime:

        def transport() -> InProcessAgentTransport:
            return InProcessAgentTransport(
                AgentProtocolServer(
                    AgentApplicationService(
                        runtime,
                        sessions,
                        requests,
                        workspace=workspace.resolve(),
                    )
                )
            )

        with ClientStateStore(
            tmp_path / "client",
            workspace_identity=str(workspace.resolve()),
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            await controller.start(StartRequest(workspace=str(workspace)))
            await controller.dispatch(CreateThreadIntent())
            await controller.dispatch(SubmitPromptIntent("读取文件"))
            waiting = await _wait_for_turn(controller, "waiting_approval")
            approval = pending_approval(waiting.thread_view)
            assert approval is not None
            before = store.state().next_command_sequence

            await controller.dispatch(LoadApprovalEvidenceIntent(approval.binding))
            assert store.state().next_command_sequence == before
            assert controller.state.approval_evidence is not None
            assert controller.state.approval_evidence.status is ApprovalEvidenceStatus.NOT_REQUIRED

            with pytest.raises(ProductUIError) as changed_fingerprint:
                await controller.dispatch(
                    RespondApprovalIntent(
                        replace(approval.binding, fingerprint="b" * 64),
                        outcome="approved",
                    )
                )
            assert changed_fingerprint.value.code == "approval_stale"
            with pytest.raises(ProductUIError) as long_reason:
                await controller.dispatch(
                    RespondApprovalIntent(
                        approval.binding,
                        outcome="approved",
                        reason="x" * 2001,
                    )
                )
            assert long_reason.value.code == "approval_decision_invalid"
            assert store.state().next_command_sequence == before

            await controller.dispatch(RespondApprovalIntent(approval.binding, outcome="approved"))
            assert store.state().next_command_sequence == before + 1
            completed = await _wait_for_turn(controller, "completed")
            assert completed.approval_evidence is None
            assert len(tools.calls) == 1

            with pytest.raises(ProductUIError) as stale:
                await controller.dispatch(
                    RespondApprovalIntent(approval.binding, outcome="approved")
                )
            assert stale.value.code == "approval_stale"
            assert store.state().next_command_sequence == before + 1
            await controller.close(deadline_seconds=2)


def _read_step(path: str, index: int) -> list[object]:
    return [
        ResponseStarted(response_id=f"response-{index}"),
        ToolCallCompleted(call_id=f"call-{index}", tool="test.read", arguments={"path": path}),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def test_session_rule_auto_approves_only_identical_calls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider(
        [
            _read_step("a", 1),
            _read_step("a", 2),
            _read_step("b", 3),
            answer("完成"),
        ]
    )
    async with AgentRuntime(sessions, provider, tools) as runtime:

        def transport() -> InProcessAgentTransport:
            return InProcessAgentTransport(
                AgentProtocolServer(
                    AgentApplicationService(
                        runtime,
                        sessions,
                        requests,
                        workspace=workspace.resolve(),
                    )
                )
            )

        with ClientStateStore(
            tmp_path / "client",
            workspace_identity=str(workspace.resolve()),
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            await controller.start(StartRequest(workspace=str(workspace)))
            await controller.dispatch(CreateThreadIntent())
            await controller.dispatch(SubmitPromptIntent("读取文件"))
            waiting = await _wait_for_turn(controller, "waiting_approval")
            first = pending_approval(waiting.thread_view)
            assert first is not None
            await controller.dispatch(LoadApprovalEvidenceIntent(first.binding))
            await controller.dispatch(
                RespondApprovalIntent(first.binding, outcome="approved", remember_for_session=True)
            )

            # 第二次调用相同，自动批准；第三次参数不同，仍需人工处理。
            async with asyncio.timeout(10):
                while True:
                    try:
                        pending = pending_approval(controller.state.thread_view)
                    except ProductUIError:
                        pending = None
                    if pending is not None and '"b"' in pending.arguments_json:
                        break
                    await asyncio.sleep(0.02)
            assert len(tools.calls) == 2
            assert controller.state.auto_accept_edits is False
            await controller.dispatch(ToggleAutoAcceptIntent())
            assert controller.state.auto_accept_edits is True
            await controller.close(deadline_seconds=2)


def _pending(approval_type: str, arguments: str = "{}") -> PendingApproval:
    return PendingApproval(
        binding=ApprovalBinding(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64),
        approval_type=approval_type,  # type: ignore[arg-type]
        policy_version="v1",
        tool="tool",
        tool_version="1",
        effect_class="non_idempotent_write",
        arguments_json=arguments,
        diff_artifact=None,
    )


def test_auto_accepting_edits_never_covers_commands() -> None:
    automation = ApprovalAutomation()
    assert not automation.allows(_pending("patch_batch"))

    automation.remember(_pending("patch"))

    assert automation.auto_accept_edits
    assert automation.allows(_pending("patch_batch"))
    assert not automation.allows(_pending("process"))


def test_session_rule_for_commands_needs_identical_arguments() -> None:
    automation = ApprovalAutomation()
    automation.remember(_pending("process", '{"command":"npm test"}'))

    assert automation.allows(_pending("process", '{"command":"npm test"}'))
    assert not automation.allows(_pending("process", '{"command":"rm -rf /"}'))
    assert not automation.auto_accept_edits


def test_each_approval_is_only_attempted_once() -> None:
    automation = ApprovalAutomation()
    pending = _pending("process")

    assert automation.claim_attempt(pending)
    assert not automation.claim_attempt(pending)


async def test_controller_question_rejects_invalid_answer_before_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="question"),
                ToolCallCompleted(
                    call_id="ask",
                    tool="ask_user",
                    arguments={"question": "选择环境", "options": ["测试", "生产"]},
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            answer("回答后完成"),
        ]
    )
    async with AgentRuntime(sessions, provider, enable_questions=True) as runtime:

        def transport() -> InProcessAgentTransport:
            return InProcessAgentTransport(
                AgentProtocolServer(
                    AgentApplicationService(
                        runtime,
                        sessions,
                        requests,
                        workspace=workspace.resolve(),
                    )
                )
            )

        with ClientStateStore(
            tmp_path / "client",
            workspace_identity=str(workspace.resolve()),
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            await controller.start(StartRequest(workspace=str(workspace)))
            await controller.dispatch(CreateThreadIntent())
            await controller.dispatch(SubmitPromptIntent("准备发布"))
            waiting = await _wait_for_turn(controller, "waiting_input")
            question = pending_question(waiting.thread_view)
            assert question is not None
            before = store.state().next_command_sequence

            with pytest.raises(ProductUIError) as invalid:
                await controller.dispatch(RespondQuestionIntent(question.binding, "   "))
            assert invalid.value.code == "question_answer_invalid"
            assert store.state().next_command_sequence == before

            await controller.dispatch(RespondQuestionIntent(question.binding, "生产"))
            assert store.state().next_command_sequence == before + 1
            await _wait_for_turn(controller, "completed")
            assert len(provider.requests) == 2
            await controller.close(deadline_seconds=2)


async def test_controller_steer_and_cancel_are_distinct_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    provider = ScriptedProvider(
        [[ResponseStarted(response_id="slow"), ResponseCompleted()]],
        delay_seconds=10,
    )
    async with AgentRuntime(sessions, provider) as runtime:

        def transport() -> InProcessAgentTransport:
            return InProcessAgentTransport(
                AgentProtocolServer(
                    AgentApplicationService(
                        runtime,
                        sessions,
                        requests,
                        workspace=workspace.resolve(),
                    )
                )
            )

        with ClientStateStore(
            tmp_path / "client",
            workspace_identity=str(workspace.resolve()),
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            await controller.start(StartRequest(workspace=str(workspace)))
            await controller.dispatch(CreateThreadIntent())
            await controller.dispatch(SubmitPromptIntent("执行慢任务"))
            control = active_turn_control(controller.state.thread_view)
            assert control is not None and control.can_steer and control.can_cancel
            before = store.state().next_command_sequence

            await controller.dispatch(SteerTurnIntent(control.binding, "先检查测试"))
            assert store.state().next_command_sequence == before + 1
            current = active_turn_control(controller.state.thread_view)
            assert current is not None
            await controller.dispatch(CancelTurnIntent(current.binding))
            assert store.state().next_command_sequence == before + 2
            await _wait_for_turn(controller, "cancelled")

            with pytest.raises(ProductUIError) as closed:
                await controller.dispatch(CancelTurnIntent(current.binding))
            assert closed.value.code == "turn_control_stale"
            assert store.state().next_command_sequence == before + 2
            await controller.close(deadline_seconds=2)
