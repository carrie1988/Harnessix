from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import FakeProvider
from harnessix.product_ui.controller import (
    ControllerPhase,
    CreateThreadIntent,
    ProductController,
    ReconnectIntent,
    StartRequest,
    SubmitPromptIntent,
)
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.contracts import PublicTextContent
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore


async def _completed_state(controller: ProductController):
    for _ in range(100):
        state = controller.state
        turn = state.thread_view.current_turn if state.thread_view is not None else None
        if turn is not None and turn.status == "completed":
            return state
        await asyncio.wait_for(controller.next_update(), timeout=2)
    raise AssertionError("Turn未进入终态")


async def test_controller_serializes_create_submit_and_explicit_reconnect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    async with AgentRuntime(sessions, FakeProvider("控制器闭环完成")) as runtime:

        def transport() -> InProcessAgentTransport:
            service = AgentApplicationService(
                runtime,
                sessions,
                requests,
                workspace=workspace.resolve(),
            )
            return InProcessAgentTransport(AgentProtocolServer(service))

        with ClientStateStore(
            tmp_path / "client",
            workspace_identity=str(workspace.resolve()),
        ) as store:
            session = RecoverableAgentSession(store, transport)
            controller = ProductController(session)
            started = await controller.start(StartRequest(workspace=str(workspace)))
            assert started.phase is ControllerPhase.READY
            assert started.threads == () and started.selected_thread_id is None

            await controller.dispatch(CreateThreadIntent())
            thread_id = controller.state.selected_thread_id
            assert thread_id is not None
            assert len(controller.state.threads) == 1

            await controller.dispatch(SubmitPromptIntent("完成控制器测试"))
            completed = await _completed_state(controller)
            assert completed.thread_view is not None
            texts = [
                item.item.content.text
                for item in completed.thread_view.items
                if isinstance(item.item.content, PublicTextContent)
            ]
            assert "完成控制器测试" in texts
            assert "控制器闭环完成" in texts

            await controller.dispatch(ReconnectIntent())
            assert controller.state.connection_generation == 2
            assert controller.state.selected_thread_id == thread_id
            assert controller.state.thread_view is not None
            assert store.state().next_command_sequence == 3

            report = await controller.close(deadline_seconds=2)
            assert report.clean and report.processed_intents == 3
            assert store.state().clean_shutdown


async def test_controller_rejects_invalid_prompt_without_consuming_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    async with AgentRuntime(sessions, FakeProvider()) as runtime:

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
            before = store.state().next_command_sequence

            with pytest.raises(ProductUIError) as error:
                await controller.dispatch(SubmitPromptIntent("   "))

            assert error.value.code == "controller_prompt_invalid"
            assert store.state().next_command_sequence == before
            assert controller.state.phase is ControllerPhase.READY
            assert controller.state.last_notice is not None
            assert controller.state.last_notice.code == "controller_prompt_invalid"
            await controller.close(deadline_seconds=2)


async def test_controller_caller_cancellation_does_not_cancel_accepted_intent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    async with AgentRuntime(sessions, FakeProvider()) as runtime:

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

            waiter = asyncio.create_task(controller.dispatch(CreateThreadIntent()))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

            for _ in range(100):
                if controller.state.selected_thread_id is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("已接纳Intent被调用者取消")
            assert store.state().next_command_sequence == 2
            await controller.close(deadline_seconds=2)


async def test_controller_close_timeout_reports_unknown_without_reusing_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    async with AgentRuntime(sessions, FakeProvider()) as runtime:

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
            session = RecoverableAgentSession(store, transport)
            controller = ProductController(session)
            await controller.start(StartRequest(workspace=str(workspace)))
            entered = asyncio.Event()

            async def blocked(*_args, **_kwargs):
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(session, "execute_prepared", blocked)
            waiter = asyncio.create_task(controller.dispatch(CreateThreadIntent()))
            await asyncio.wait_for(entered.wait(), timeout=1)

            report = await controller.close(deadline_seconds=0.01)

            with pytest.raises(ProductUIError) as error:
                await waiter
            assert error.value.code == "controller_operation_unknown"
            assert report.error_code == "controller_close_timeout" and not report.clean
            assert store.state().next_command_sequence == 2
            assert await controller.close(deadline_seconds=1) == report
