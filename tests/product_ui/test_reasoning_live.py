"""思考预览经真实运行时、协议服务端、SDK客户端到界面投影的端到端链路（0.10.0b9）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from textual.widgets import Input

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ReasoningDelta,
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
)
from harnessix.product_ui.app import ProductApp
from harnessix.product_ui.controller import ProductController, StartRequest
from harnessix.product_ui.rendering import transcript_lines
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.product_ui.test_app_interactions import _turn_status, _wait_until


class ThinkingProvider:
    """先推送思考内容，然后停住，直到测试放行才给出回复。"""

    def __init__(self) -> None:
        self.thinking = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        yield ResponseStarted(response_id="r")
        yield ReasoningDelta(delta="先看看用户要什么。\n")
        yield ReasoningDelta(delta="拓扑排序可以用 Kahn 算法。")
        self.thinking.set()
        await self.release.wait()
        yield TextStarted(content_id="t")
        yield TextCompleted(content_id="t", text="好的")
        yield ResponseCompleted()


def _roles(controller: ProductController) -> list[tuple[str, str]]:
    return [(line.role, line.text) for line in transcript_lines(controller.state.thread_view)]


async def test_reasoning_shows_while_thinking_and_disappears_after(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    provider = ThinkingProvider()
    async with AgentRuntime(sessions, provider) as runtime:

        def transport() -> InProcessAgentTransport:
            return InProcessAgentTransport(
                AgentProtocolServer(
                    AgentApplicationService(
                        runtime, sessions, requests, workspace=workspace.resolve()
                    )
                )
            )

        with ClientStateStore(
            tmp_path / "client", workspace_identity=str(workspace.resolve())
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            async with app.run_test(size=(100, 30)) as pilot:
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                await pilot.press("h", "i", "enter")
                await asyncio.wait_for(provider.thinking.wait(), 10)
                await _wait_until(lambda: any(role == "思考" for role, _ in _roles(controller)))
                expected = ("思考", "先看看用户要什么。 拓扑排序可以用 Kahn 算法。")
                assert expected in _roles(controller)
                provider.release.set()
                await _wait_until(lambda: _turn_status(controller) == "completed")
                await _wait_until(lambda: all(role != "思考" for role, _ in _roles(controller)))
                assert ("Harnessix", "好的") in _roles(controller)
