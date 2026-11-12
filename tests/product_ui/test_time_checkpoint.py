"""运行时间检查点：实际运行满一段时间后询问；无人回应才暂停；等待审批的时间不计（ADR 0148）。

真实试用里第一轮在整60分钟时被内核时限判为“未完成”，其中包含14次等待审批的时间，用户只能输入“继续任务”。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from textual.widgets import Button, Input

from harnessix.agent.models import TextContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui import turn_pacing
from harnessix.product_ui.app import ProductApp
from harnessix.product_ui.controller import ProductController, StartRequest
from harnessix.product_ui.interaction_screens import CheckpointScreen
from harnessix.product_ui.session import RecoverableAgentSession
from harnessix.product_ui.state_store import ClientStateStore
from harnessix.product_ui.turn_budget import (
    PRODUCT_TURN_BUDGET,
    interactive_turn_budget,
    turn_budget_for,
)
from harnessix.product_ui.turn_checkpoint import (
    CHECKPOINT_SECONDS,
    CONTINUE_PROMPT,
    INTERACTIVE_TIMEOUT_SECONDS,
    WRAP_UP_PROMPT,
    ActiveTimeMeter,
)
from harnessix.product_ui.turn_pacing import WRAP_UP_MESSAGE
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.sdk import InProcessAgentTransport
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.product_ui.test_app_interactions import (
    _click_when_ready,
    _turn_status,
    _wait_until,
)


def test_only_running_time_counts_towards_the_checkpoint() -> None:
    meter, turn = ActiveTimeMeter(limit=60.0), uuid4()
    meter.observe(turn, "calling_model", 0.0)
    meter.observe(turn, "executing_tools", 20.0)
    meter.observe(turn, "waiting_approval", 30.0)
    # 等了用户十分钟：这段不计。
    meter.observe(turn, "executing_tools", 630.0)
    meter.observe(turn, "calling_model", 640.0)
    assert meter.active == 40.0 and not meter.due
    meter.observe(turn, "waiting_input", 665.0)
    meter.observe(turn, "completed", 900.0)
    # 界面暂时没有会话视图：不计时，也不清零。
    meter.observe(None, None, 1000.0)
    assert meter.active == 65.0 and meter.due
    meter.restart()
    assert meter.active == 0.0 and not meter.due


def test_a_new_turn_starts_counting_from_zero() -> None:
    meter, first, second = ActiveTimeMeter(limit=60.0), uuid4(), uuid4()
    meter.observe(first, "calling_model", 0.0)
    meter.observe(first, "calling_model", 50.0)
    meter.observe(second, "calling_model", 55.0)
    meter.observe(second, "calling_model", 70.0)
    assert meter.active == 15.0 and not meter.due


def test_interactive_turns_leave_the_time_limit_to_the_checkpoint() -> None:
    assert CHECKPOINT_SECONDS == 3600.0
    for pricing, visible in (("free", False), ("unknown", True), ("unknown", False)):
        assert (
            interactive_turn_budget(pricing, cost_visible=visible).timeout_seconds
            == INTERACTIVE_TIMEOUT_SECONDS
        )
    # 无人值守（exec）保持原来的一小时。
    assert PRODUCT_TURN_BUDGET.timeout_seconds == 3600.0
    assert turn_budget_for("free").timeout_seconds == 3600.0


async def _session(tmp_path: Path, provider: ScriptedProvider, drive) -> list[tuple[str, str]]:
    """用真实的Runtime、协议服务和TUI跑一遍，返回会话里全部用户与助手正文。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    requests = SQLiteProtocolRequestStore(sessions.path)
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
                await drive(app, pilot, controller)
                thread_id = controller.state.selected_thread_id
                assert thread_id is not None
        thread = await sessions.get_thread(thread_id)
        return [
            (item.content.kind, item.content.text)
            for turn in thread.turns
            for item in turn.items
            if isinstance(item.content, TextContent)
        ]


@pytest.fixture
def quick_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """把60分钟缩到半秒、定时器缩到0.1秒，其余逻辑与产品相同。"""
    monkeypatch.setattr(turn_pacing, "CHECKPOINT_TICK_SECONDS", 0.1)
    monkeypatch.setattr(turn_pacing, "ActiveTimeMeter", lambda: ActiveTimeMeter(limit=0.5))


async def test_answering_continue_in_time_leaves_the_task_untouched(
    tmp_path: Path, quick_checkpoint: None
) -> None:
    provider = ScriptedProvider([answer("做完了。")], delay_seconds=0.6)

    async def drive(app: ProductApp, pilot, controller: ProductController) -> None:
        await pilot.press("g", "o", "enter")
        await _wait_until(lambda: isinstance(app.screen, CheckpointScreen))
        await _click_when_ready(pilot, "#checkpoint-continue")
        await _wait_until(lambda: not isinstance(app.screen, CheckpointScreen))
        await _wait_until(lambda: _turn_status(controller) == "completed", timeout_seconds=30)

    texts = await _session(tmp_path, provider, drive)
    # 同一轮正常完成：没有取消、没有新的一轮、没有额外的用户消息。
    assert texts == [("user_message", "go"), ("assistant_message", "做完了。")]
    assert len(provider.requests) == 1


async def test_the_question_closes_by_itself_when_the_task_finishes_first(
    tmp_path: Path, quick_checkpoint: None
) -> None:
    provider = ScriptedProvider([answer("做完了。")], delay_seconds=1.2)

    async def drive(app: ProductApp, pilot, controller: ProductController) -> None:
        await pilot.press("g", "o", "enter")
        await _wait_until(lambda: isinstance(app.screen, CheckpointScreen))
        # 用户没有选择，任务自己做完了：询问不再占着屏幕，也不把已完成的任务当成暂停。
        await _wait_until(lambda: not isinstance(app.screen, CheckpointScreen))
        assert _turn_status(controller) == "completed"

    texts = await _session(tmp_path, provider, drive)
    assert texts == [("user_message", "go"), ("assistant_message", "做完了。")]
    assert len(provider.requests) == 1


async def test_wrap_up_choice_sends_the_wrap_up_note_into_the_running_turn(
    tmp_path: Path, quick_checkpoint: None
) -> None:
    provider = ScriptedProvider(
        [answer("我再检查一遍。"), answer("总结：已完成。")], delay_seconds=0.6
    )

    async def drive(app: ProductApp, pilot, controller: ProductController) -> None:
        await pilot.press("g", "o", "enter")
        await _wait_until(lambda: isinstance(app.screen, CheckpointScreen))
        await _click_when_ready(pilot, "#checkpoint-wrap-up")
        await _wait_until(lambda: _turn_status(controller) == "completed", timeout_seconds=30)

    texts = await _session(tmp_path, provider, drive)
    assert ("user_message", WRAP_UP_MESSAGE) in texts
    assert texts[-1] == ("assistant_message", "总结：已完成。")


@pytest.mark.parametrize(
    ("button", "prompt"),
    [("#checkpoint-continue", CONTINUE_PROMPT), ("#checkpoint-wrap-up", WRAP_UP_PROMPT)],
)
async def test_an_unanswered_checkpoint_pauses_and_the_choice_starts_a_new_turn(
    tmp_path: Path, quick_checkpoint: None, monkeypatch: pytest.MonkeyPatch, button, prompt
) -> None:
    monkeypatch.setattr(ProductApp, "checkpoint_pause_seconds", 0.4)
    provider = ScriptedProvider([answer("很慢的一步。")], delay_seconds=3.0)

    async def drive(app: ProductApp, pilot, controller: ProductController) -> None:
        await pilot.press("g", "o", "enter")
        await _wait_until(lambda: isinstance(app.screen, CheckpointScreen))
        screen = app.screen
        assert isinstance(screen, CheckpointScreen)
        # 没有人回应：本轮被暂停，询问仍然开着。
        await _wait_until(lambda: screen.paused and _turn_status(controller) == "cancelled")
        assert isinstance(app.screen, CheckpointScreen)
        provider.steps = (answer("接着做完了。"),)
        provider.delay_seconds = 0
        await _click_when_ready(pilot, button)
        await _wait_until(lambda: _turn_status(controller) == "completed", timeout_seconds=30)

    texts = await _session(tmp_path, provider, drive)
    assert ("user_message", prompt) in texts
    assert texts[-1] == ("assistant_message", "接着做完了。")


async def test_stop_after_a_pause_leaves_the_task_stopped(
    tmp_path: Path, quick_checkpoint: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProductApp, "checkpoint_pause_seconds", 0.4)
    provider = ScriptedProvider([answer("很慢的一步。")], delay_seconds=3.0)

    async def drive(app: ProductApp, pilot, controller: ProductController) -> None:
        await pilot.press("g", "o", "enter")
        await _wait_until(lambda: isinstance(app.screen, CheckpointScreen))
        screen = app.screen
        assert isinstance(screen, CheckpointScreen)
        await _wait_until(lambda: screen.paused and _turn_status(controller) == "cancelled")
        assert screen.query_one("#checkpoint-stop", Button) is not None
        await pilot.press("escape")
        await _wait_until(lambda: not isinstance(app.screen, CheckpointScreen))
        await pilot.pause(0.5)
        assert _turn_status(controller) == "cancelled"

    texts = await _session(tmp_path, provider, drive)
    assert [kind for kind, _ in texts].count("user_message") == 1
    assert len(provider.requests) == 1
