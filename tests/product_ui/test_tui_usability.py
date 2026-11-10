"""0.10.0b4易用性修复：审批自动弹窗、情境快捷键、白话状态栏、只追加的对话渲染与审批摘要。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import Input, RichLog, Static

from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui import interaction_presenter
from harnessix.product_ui.app import ProductApp
from harnessix.product_ui.controller import (
    ControllerPhase,
    ProductController,
    ProductControllerState,
    StartRequest,
)
from harnessix.product_ui.interaction_screens import ApprovalScreen, approval_summary
from harnessix.product_ui.interactions import ApprovalBinding, PendingApproval
from harnessix.product_ui.main_view import ProductMainView, _status_text
from harnessix.product_ui.projection import (
    ProductViewState,
    ProjectedItem,
    TurnProjection,
)
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.protocol.contracts import PublicItem, PublicTextContent, ThreadView
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.product_ui.test_app_interactions import _turn_status, _wait_until

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_a_new_approval_opens_by_itself_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(interaction_presenter, "AUTO_PROMPT_DELAY_SECONDS", 0.2)
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
                assert not app.check_action("approval", ())
                assert not app.check_action("cancel_turn", ())
                await pilot.press("r", "e", "a", "d", "enter")
                await _wait_until(lambda: _turn_status(controller) == "waiting_approval")
                # 不按任何键，审批窗口自己弹出。
                await _wait_until(lambda: isinstance(app.screen, ApprovalScreen))
                await pilot.press("escape")
                await _wait_until(lambda: not isinstance(app.screen, ApprovalScreen))
                await pilot.pause(0.6)
                # 选了暂不处理后不再反复弹出；Ctrl+A仍可打开。
                assert not isinstance(app.screen, ApprovalScreen)
                assert app.check_action("approval", ())
                await pilot.press("ctrl+a")
                await _wait_until(lambda: isinstance(app.screen, ApprovalScreen))
                await pilot.click("#approval-approve")
                await _wait_until(lambda: _turn_status(controller) == "completed")
                await _wait_until(lambda: not app.check_action("approval", ()))
                assert len(tools.calls) == 1


def _thread() -> ThreadView:
    return ThreadView(
        thread_id=uuid4(),
        workspace="/workspace",
        cursor=1,
        turn_count=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _text(kind: str, text: str, *, final: bool = True) -> ProjectedItem:
    return ProjectedItem(
        item=PublicItem(
            item_id=uuid4(),
            status="completed" if final else "started",
            content=PublicTextContent(kind=kind, text=text),
        ),
        turn_id=uuid4(),
        first_cursor=1,
        last_cursor=1,
        final=final,
    )


def _state(view: ProductViewState, revision: int) -> ProductControllerState:
    return ProductControllerState(
        phase=ControllerPhase.READY,
        threads=(view.thread,),
        selected_thread_id=view.thread.thread_id,
        thread_view=view,
        revision=revision,
    )


async def test_settled_lines_are_appended_without_redrawing_history() -> None:
    thread = _thread()
    first = ProductViewState(
        thread=thread,
        durable_cursor=1,
        items=(_text("user_message", "检查项目"), _text("assistant_message", "**完成**")),
    )
    app: App[None] = App()
    widget = ProductMainView()
    async with app.run_test(size=(120, 30)) as pilot:
        await app.mount(widget)
        log = app.query_one("#transcript", RichLog)
        clears = 0
        original_clear = log.clear

        def counting_clear() -> RichLog:
            nonlocal clears
            clears += 1
            return original_clear()

        log.clear = counting_clear  # type: ignore[method-assign]
        assert await widget.render_state(_state(first, 1))
        await pilot.pause()
        rendered = "\n".join(line.text for line in log.lines)
        # Agent回复按Markdown排版，不显示星号。
        assert "完成" in rendered and "**" not in rendered
        settled_clears = clears
        second = replace(first, items=(*first.items, _text("user_message", "再跑一次测试")))
        assert await widget.render_state(_state(second, 2))
        await pilot.pause()
        assert clears == settled_clears
        assert "再跑一次测试" in "\n".join(line.text for line in log.lines)


async def test_unsettled_lines_stay_in_the_live_area() -> None:
    thread = _thread()
    view = ProductViewState(
        thread=thread,
        durable_cursor=1,
        items=(
            _text("user_message", "检查项目"),
            _text("assistant_message", "处理中", final=False),
        ),
    )
    app: App[None] = App()
    widget = ProductMainView()
    async with app.run_test(size=(120, 30)) as pilot:
        await app.mount(widget)
        assert await widget.render_state(_state(view, 1))
        await pilot.pause()
        history = "\n".join(line.text for line in app.query_one("#transcript", RichLog).lines)
        assert "检查项目" in history and "处理中" not in history
        console = Console(width=120, record=True)
        console.print(app.query_one("#live", Static).content)
        assert "处理中" in console.export_text()


def test_status_bar_speaks_plain_language() -> None:
    thread = _thread()
    turn = TurnProjection(
        turn_id=uuid4(),
        request_id="r",
        status="calling_model",
        budget=None,
        usage=None,
        model_step=1,
    )
    view = ProductViewState(thread=thread, durable_cursor=1, current_turn=turn)
    status = _status_text(_state(view, 1))
    assert status.startswith("思考中")
    assert "连接代际" not in status and "Turn" not in status and "calling_model" not in status
    waiting = replace(view, current_turn=replace(turn, status="waiting_approval"))
    assert _status_text(_state(waiting, 2)).startswith("等待你审批（Ctrl+A）")
    broken = ProductControllerState(phase=ControllerPhase.BROKEN)
    assert _status_text(broken).startswith("连接中断，按 Ctrl+R 重新连接")
    assert _status_text(ProductControllerState(phase=ControllerPhase.READY)) == "就绪"


def _pending(tool: str, arguments: str, effect: str = "idempotent_write") -> PendingApproval:
    return PendingApproval(
        binding=ApprovalBinding(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64),
        approval_type="tool",
        policy_version="policy-v1",
        tool=tool,
        tool_version="1",
        effect_class=effect,  # type: ignore[arg-type]
        arguments_json=arguments,
        diff_artifact=None,
    )


def test_approval_summary_says_what_the_agent_wants_to_do() -> None:
    patch = _pending("apply_patch_batch", '{"files":[{"path":"a.py"},{"path":"b.py"}]}')
    assert approval_summary(patch) == "Agent 请求：修改文件：a.py, b.py"
    command = _pending(
        "host_command.powershell", '{"command":"Remove-Item build -Recurse"}', "destructive"
    )
    assert approval_summary(command) == (
        "Agent 请求：运行（沙箱外）：Remove-Item build -Recurse\n"
        "注意：此操作可能删除或覆盖数据，请仔细核对。"
    )
    assert approval_summary(_pending("custom.tool", "not json")) == "Agent 请求：调用 custom.tool"
