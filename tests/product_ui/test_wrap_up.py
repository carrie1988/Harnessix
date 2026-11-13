"""一键收尾（Ctrl+F）：任务其实已经做完、模型还在自己加验证时，不用打字就能让它总结并结束。"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Input

from harnessix.agent.models import ApprovalRequestContent, TextContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.wrap_up import WrapUpToolView
from harnessix.product_ui.app import ProductApp, action_enabled
from harnessix.product_ui.controller import ProductController, ProductControllerState, StartRequest
from harnessix.product_ui.interaction_screens import ApprovalScreen, SteerScreen
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.product_ui.turn_pacing import WRAP_UP_MESSAGE
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.product_ui.test_app_interactions import _turn_status, _wait_until


async def test_wrap_up_key_sends_the_fixed_note_without_a_dialog(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    provider = ScriptedProvider(
        [answer("我再检查一遍着色器。"), answer("已完成：总结如下。")], delay_seconds=0.8
    )
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
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                # 没有任务在运行时这个键不可用，也不显示在底部快捷键栏。
                assert not app.check_action("wrap_up_turn", ())
                await pilot.press("g", "o", "enter")
                await _wait_until(
                    lambda: _turn_status(controller) in {"preparing_context", "calling_model"}
                )
                assert app.check_action("wrap_up_turn", ())
                await pilot.press("ctrl+f")
                await _wait_until(lambda: not app._intent_in_flight)
                assert not isinstance(app.screen, SteerScreen)
                await _wait_until(
                    lambda: _turn_status(controller) == "completed", timeout_seconds=30
                )
                thread_id = controller.state.selected_thread_id
                assert thread_id is not None

        # 收尾说明作为用户的补充输入进入本轮，模型在下一步看到它并给出总结。
        turn = (await sessions.get_thread(thread_id)).turns[-1]
        texts = [
            (item.content.kind, item.content.text)
            for item in turn.items
            if isinstance(item.content, TextContent)
        ]
        assert ("user_message", WRAP_UP_MESSAGE) in texts
        assert texts[-1] == ("assistant_message", "已完成：总结如下。")
        assert len(provider.requests) == 2


async def test_wrap_up_rejects_an_approval_still_waiting_for_its_dialog(tmp_path: Path) -> None:
    """Ctrl+F时调用已经提出、正在等审批而窗口还没弹出（Beta Issue #4，发布会话Windows真机3/3）。

    此前窗口照常弹出、批准后命令照样执行；现在这条审批直接被拒绝，不再弹窗，模型以总结结束。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
    tools = RecordingTools(approval=True)
    provider = ScriptedProvider([tool_step("test.read"), answer("已完成：总结如下。")])
    async with AgentRuntime(sessions, provider, tools, model_tool_view=WrapUpToolView()) as runtime:

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
                composer = app.query_one("#composer", Input)
                await _wait_until(lambda: not composer.disabled)
                await pilot.press("g", "o", "enter")
                await _wait_until(lambda: _turn_status(controller) == "waiting_approval")
                # 审批窗口要等AUTO_PROMPT_DELAY_SECONDS才弹出，这之前按下Ctrl+F。
                assert not isinstance(app.screen, ApprovalScreen)
                await pilot.press("ctrl+f")
                await _wait_until(
                    lambda: _turn_status(controller) == "completed", timeout_seconds=30
                )
                assert not isinstance(app.screen, ApprovalScreen)
                thread_id = controller.state.selected_thread_id
                assert thread_id is not None

        turn = (await sessions.get_thread(thread_id)).turns[-1]
    assert tools.calls == []
    decisions = [
        item.content for item in turn.items if isinstance(item.content, ApprovalRequestContent)
    ]
    assert [d.decision.outcome.value for d in decisions if d.decision] == ["rejected"]
    texts = [
        (item.content.kind, item.content.text)
        for item in turn.items
        if isinstance(item.content, TextContent)
    ]
    assert texts[-1] == ("assistant_message", "已完成：总结如下。")


def test_wrap_up_is_a_main_screen_action_hidden_behind_dialogs() -> None:
    idle = ProductControllerState()
    assert not action_enabled("wrap_up_turn", idle, modal_open=False)
    assert not action_enabled("wrap_up_turn", idle, modal_open=True)
