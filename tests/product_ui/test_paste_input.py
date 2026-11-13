"""多行粘贴不再只剩第一行（Beta Issue #19）。

0.10.0b32 Windows（ConPTY，bracketed paste）：把三行文本粘贴进输入框，只插入第一行，其余静默丢弃；
Textual的单行Input对粘贴只取`splitlines()[0]`，三个平台的终端都一样。现在多行粘贴显示为一个标记，提交时还原原文。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Input

from harnessix.agent.models import TextContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.app import ProductApp
from harnessix.product_ui.controller import ProductController, StartRequest
from harnessix.product_ui.interaction_screens import SteerScreen
from harnessix.product_ui.interactions import ActiveTurnControl, TurnBinding
from harnessix.product_ui.paste_input import PasteAwareInput, clear_input
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.product_ui.test_app_interactions import _wait_until

ISSUE = "HARNESSIX-FIRST-LINE\r\nHARNESSIX-SECOND-LINE\r\nHARNESSIX-THIRD-LINE"
EXPECTED = "HARNESSIX-FIRST-LINE\nHARNESSIX-SECOND-LINE\nHARNESSIX-THIRD-LINE"


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield PasteAwareInput(id="field")


async def _paste(pilot, widget: Input, text: str) -> None:  # type: ignore[no-untyped-def]
    widget.post_message(events.Paste(text))
    await pilot.pause()


async def test_a_multi_line_paste_keeps_every_line() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        field = app.query_one(PasteAwareInput)
        field.focus()
        field.value = "请修复："
        field.cursor_position = len(field.value)
        await _paste(pilot, field, ISSUE)
        assert field.value == "请修复：[粘贴的文本 #1：3 行]"
        assert field.full_value == "请修复：" + EXPECTED
        # 单行粘贴照旧直接插入。
        await _paste(pilot, field, " 谢谢")
        assert field.full_value == "请修复：" + EXPECTED + " 谢谢"


async def test_two_pastes_are_numbered_and_a_deleted_one_is_dropped() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        field = app.query_one(PasteAwareInput)
        field.focus()
        await _paste(pilot, field, "a\nb")
        await _paste(pilot, field, "c\nd\ne\n")
        assert field.value == "[粘贴的文本 #1：2 行][粘贴的文本 #2：3 行]"
        assert field.full_value == "a\nbc\nd\ne\n"
        field.value = field.value.replace("[粘贴的文本 #1：2 行]", "")
        assert field.full_value == "c\nd\ne\n"
        clear_input(field)
        assert field.value == "" and field.full_value == ""


async def test_the_task_box_sends_the_whole_paste_to_the_agent(tmp_path: Path) -> None:
    """真实的产品界面：粘贴三行后按Enter，Agent收到的用户消息是完整的三行。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    provider = ScriptedProvider([answer("好的")])
    async with AgentRuntime(sessions, provider) as runtime:

        def transport() -> InProcessAgentTransport:
            service = AgentApplicationService(
                runtime, sessions, requests, workspace=workspace.resolve()
            )
            return InProcessAgentTransport(AgentProtocolServer(service))

        with ClientStateStore(
            tmp_path / "client", workspace_identity=str(workspace.resolve())
        ) as store:
            controller = ProductController(RecoverableAgentSession(store, transport))
            app = ProductApp(controller, StartRequest(workspace=str(workspace)))
            async with app.run_test(size=(120, 30)) as pilot:
                composer = app.query_one("#composer", PasteAwareInput)
                await _wait_until(lambda: not composer.disabled)
                composer.focus()
                await _paste(pilot, composer, ISSUE)
                assert composer.value == "[粘贴的文本 #1：3 行]"
                await pilot.press("enter")
                await _wait_until(lambda: bool(provider.requests))
                await _wait_until(lambda: composer.value == "")
    sent = [
        part.content.text
        for part in provider.requests[0].history
        if isinstance(getattr(part, "content", None), TextContent)
        and part.content.kind == "user_message"
    ]
    assert sent == [EXPECTED]


async def test_the_steer_dialog_sends_the_whole_paste() -> None:
    control = ActiveTurnControl(
        TurnBinding(uuid4(), uuid4()), status="calling_model", can_cancel=True, can_steer=True
    )
    app: App[None] = App()
    results: list[str | None] = []
    async with app.run_test(size=(80, 24)) as pilot:
        screen = SteerScreen(control)
        await app.push_screen(screen, callback=results.append)
        field = screen.query_one("#steer-text", PasteAwareInput)
        field.focus()
        await _paste(pilot, field, ISSUE)
        await pilot.press("enter")
        await pilot.pause()
    assert results == [EXPECTED]
