from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.app import ProductApp
from harnessix.product_ui.controller import (
    CreateThreadIntent,
    ProductController,
    StartRequest,
    SubmitPromptIntent,
)
from harnessix.product_ui.interaction_screens import ApprovalScreen, QuestionScreen, SteerScreen
from harnessix.product_ui.interactions import active_turn_control
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step


class _RecordingTransport(InProcessAgentTransport):
    def __init__(self, server: AgentProtocolServer, methods: list[str]) -> None:
        super().__init__(server)
        self._methods = methods

    async def exchange(self, frame: bytes) -> tuple[bytes, ...]:
        wire = json.loads(frame)
        self._methods.append(wire["method"])
        return await super().exchange(frame)


async def _wait_until(predicate, *, timeout_seconds: float = 10) -> None:
    async with asyncio.timeout(timeout_seconds):
        while not predicate():  # noqa: ASYNC110 - Textual状态由后台Controller更新
            await asyncio.sleep(0.02)


def _turn_status(controller: ProductController) -> str | None:
    view = controller.state.thread_view
    return None if view is None or view.current_turn is None else view.current_turn.status


async def test_product_app_completes_approval_modal_flow(tmp_path: Path) -> None:
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
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("ctrl+n")
                await _wait_until(lambda: controller.state.selected_thread_id is not None)
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                composer.focus()
                await pilot.press("r", "e", "a", "d", "enter")
                try:
                    await _wait_until(lambda: _turn_status(controller) == "waiting_approval")
                except TimeoutError:
                    notice = controller.state.last_notice
                    raise AssertionError(
                        f"approval_submit_timeout phase={controller.state.phase.value} "
                        f"turn={_turn_status(controller)} "
                        f"notice={None if notice is None else notice.code} "
                        f"command_sequence={store.state().next_command_sequence} "
                        f"intent_in_flight={app._intent_in_flight} "
                        f"composer_disabled={composer.disabled} "
                        f"composer_chars={len(composer.value)} "
                        f"ui_error={app._last_error_code}"
                    ) from None
                assert composer.disabled

                await pilot.press("ctrl+a")
                await _wait_until(lambda: isinstance(app.screen, ApprovalScreen))
                screen = app.screen
                assert isinstance(screen, ApprovalScreen)
                assert not screen.query_one("#approval-approve", Button).disabled
                await pilot.press("escape")
                await _wait_until(lambda: not isinstance(app.screen, ApprovalScreen))
                await _wait_until(lambda: not app._intent_in_flight)
                assert store.state().next_command_sequence == 3
                assert _turn_status(controller) == "waiting_approval"

                await pilot.press("ctrl+a")
                await _wait_until(lambda: isinstance(app.screen, ApprovalScreen))
                await pilot.click("#approval-approve")
                await _wait_until(lambda: _turn_status(controller) == "completed")

                assert len(tools.calls) == 1
                assert store.state().next_command_sequence == 4


async def test_product_app_answers_question_from_dedicated_modal(tmp_path: Path) -> None:
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
            answer("问题已处理"),
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
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("ctrl+n")
                await _wait_until(lambda: controller.state.selected_thread_id is not None)
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                composer.focus()
                await pilot.press("q", "enter")
                await _wait_until(lambda: _turn_status(controller) == "waiting_input")
                await _wait_until(
                    lambda: "待回答 Ctrl+U" in str(app.query_one("#status", Static).content)
                )

                status = str(app.query_one("#status", Static).content)
                assert "Tokens 0/" in status
                assert "费用未知（price_not_exposed）" in status
                assert "待回答 Ctrl+U" in status

                await pilot.press("ctrl+u")
                await _wait_until(lambda: isinstance(app.screen, QuestionScreen))
                await pilot.press("escape")
                await _wait_until(lambda: not isinstance(app.screen, QuestionScreen))
                await _wait_until(lambda: not app._intent_in_flight)
                assert store.state().next_command_sequence == 3
                assert _turn_status(controller) == "waiting_input"

                await pilot.press("ctrl+u")
                await _wait_until(lambda: isinstance(app.screen, QuestionScreen))
                question = app.screen
                assert isinstance(question, QuestionScreen)
                question.query_one("#question-answer", Input).value = "2"
                await pilot.press("enter")
                await _wait_until(lambda: _turn_status(controller) == "completed")
                assert store.state().next_command_sequence == 4


async def test_product_app_steers_then_explicitly_cancels_active_turn(tmp_path: Path) -> None:
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
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("ctrl+n")
                await _wait_until(lambda: controller.state.selected_thread_id is not None)
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                composer.focus()
                await pilot.press("s", "l", "o", "w", "enter")
                await _wait_until(
                    lambda: (
                        _turn_status(controller)
                        in {"accepted", "preparing_context", "calling_model"}
                    )
                )
                await pilot.press("ctrl+s")
                await _wait_until(lambda: isinstance(app.screen, SteerScreen))
                await pilot.press("escape")
                await _wait_until(lambda: not isinstance(app.screen, SteerScreen))
                await _wait_until(lambda: not app._intent_in_flight)
                assert store.state().next_command_sequence == 3

                await pilot.press("ctrl+s")
                await _wait_until(lambda: isinstance(app.screen, SteerScreen))
                steer = app.screen
                assert isinstance(steer, SteerScreen)
                steer.query_one("#steer-text", Input).value = "先运行测试"
                await pilot.press("enter")
                await _wait_until(lambda: store.state().next_command_sequence == 4)
                await _wait_until(lambda: not app._intent_in_flight)
                await pilot.press("ctrl+x")
                await _wait_until(lambda: _turn_status(controller) == "cancelled")
                assert store.state().next_command_sequence == 5


async def test_product_app_rejects_steer_from_stale_modal_without_command(tmp_path: Path) -> None:
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
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("ctrl+n")
                await _wait_until(lambda: controller.state.selected_thread_id is not None)
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                composer.focus()
                await pilot.press("s", "l", "o", "w", "enter")
                await _wait_until(
                    lambda: active_turn_control(controller.state.thread_view) is not None
                )
                control = active_turn_control(controller.state.thread_view)
                assert control is not None

                await pilot.press("ctrl+s")
                await _wait_until(lambda: isinstance(app.screen, SteerScreen))
                await runtime.cancel(control.binding.thread_id, control.binding.turn_id)
                await _wait_until(lambda: _turn_status(controller) == "cancelled")
                screen = app.screen
                assert isinstance(screen, SteerScreen)
                screen.query_one("#steer-text", Input).value = "已经过期的补充输入"
                await pilot.press("enter")
                await _wait_until(lambda: not isinstance(app.screen, SteerScreen))
                await _wait_until(lambda: not app._intent_in_flight)

                assert store.state().next_command_sequence == 3
                assert "turn_control_stale" in str(app.query_one("#status", Static).content)


async def test_product_quit_does_not_send_turn_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    provider = ScriptedProvider(
        [[ResponseStarted(response_id="slow"), ResponseCompleted()]],
        delay_seconds=10,
    )
    methods: list[str] = []
    async with AgentRuntime(sessions, provider) as runtime:

        def transport() -> InProcessAgentTransport:
            return _RecordingTransport(
                AgentProtocolServer(
                    AgentApplicationService(
                        runtime,
                        sessions,
                        requests,
                        workspace=workspace.resolve(),
                    )
                ),
                methods,
            )

        with ClientStateStore(
            tmp_path / "client",
            workspace_identity=str(workspace.resolve()),
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            exits = []
            monkeypatch.setattr(app, "exit", exits.append)
            await controller.start(StartRequest(workspace=str(workspace)))
            await controller.dispatch(CreateThreadIntent())
            await controller.dispatch(SubmitPromptIntent("slow"))
            assert active_turn_control(controller.state.thread_view) is not None

            await app._close_and_exit()

            assert len(exits) == 1 and exits[0].clean
            assert any(
                binding.key == "ctrl+q" and binding.action == "quit_product"
                for binding in ProductApp.BINDINGS
            )

    assert "turn/start" in methods
    assert "turn/cancel" not in methods
