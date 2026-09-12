from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from textual.widgets import Input, ListView

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import FakeProvider
from harnessix.product_ui.app import ProductApp
from harnessix.product_ui.controller import ControllerPhase, ProductController, StartRequest
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore


async def _wait_until(predicate, *, timeout_seconds: float = 10) -> None:
    async with asyncio.timeout(timeout_seconds):
        while not predicate():  # noqa: ASYNC110 - Textual视图没有可订阅完成事件
            await asyncio.sleep(0.02)


async def test_product_app_drives_session_picker_composer_and_resize(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    async with AgentRuntime(sessions, FakeProvider("界面闭环完成")) as runtime:

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
                await pilot.pause()
                assert app.query_one("#composer", Input).disabled

                await pilot.press("ctrl+n")
                await _wait_until(lambda: len(controller.state.threads) == 1)
                await _wait_until(lambda: not app.query_one("#composer", Input).disabled)

                stale = replace(
                    controller.state,
                    phase=ControllerPhase.STARTING,
                    revision=controller.state.revision - 1,
                )
                await app._render_state(stale)
                assert not app.query_one("#composer", Input).disabled

                first_thread = controller.state.selected_thread_id
                app.action_new_thread()
                await _wait_until(lambda: len(controller.state.threads) == 2)
                assert controller.state.selected_thread_id != first_thread

                thread_list = app.query_one("#threads", ListView)
                thread_list.index = 1
                await pilot.press("enter")
                await _wait_until(lambda: controller.state.selected_thread_id == first_thread)

                composer = app.query_one("#composer", Input)
                composer.focus()
                await pilot.press("h", "e", "l", "l", "o", "enter", "enter")
                await _wait_until(
                    lambda: (
                        controller.state.thread_view is not None
                        and controller.state.thread_view.current_turn is not None
                        and controller.state.thread_view.current_turn.status == "completed"
                    )
                )
                assert store.state().next_command_sequence == 4

                await pilot.resize_terminal(50, 20)
                await pilot.pause()
                assert app.size.width == 50 and app.size.height == 20

            assert controller.state.phase is ControllerPhase.CLOSED
