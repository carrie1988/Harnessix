"""Textual产品壳：只处理布局、输入Intent和不可变状态渲染。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, Header, Input, ListView

from harnessix.product_ui.controller import (
    CloseReport,
    ControllerPhase,
    CreateThreadIntent,
    ProductController,
    ProductControllerState,
    ProductIntent,
    ReconnectIntent,
    SelectThreadIntent,
    StartRequest,
    SubmitPromptIntent,
)
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_presenter import InteractionPresenter
from harnessix.product_ui.interactions import (
    active_turn_control,
    pending_approval,
    pending_question,
)
from harnessix.product_ui.main_view import ProductMainView
from harnessix.product_ui.paste_input import clear_input, submitted_text
from harnessix.product_ui.turn_pacing import TurnPacing

PRODUCT_QUIT_DEADLINE_SECONDS = 20.0


class ControllerUpdated(Message):
    """把Controller快照送回Textual消息循环。"""

    def __init__(self, state: ProductControllerState) -> None:
        super().__init__()
        self.state = state


# 输入框自带Ctrl+A/E/U/X（行首、行尾、删除、剪切）；设为优先，输入框有焦点时界面快捷键照样生效。
_BINDINGS: list[Binding | tuple[str, str] | tuple[str, str, str]] = [
    Binding("ctrl+a", "approval", "审批", priority=True),
    Binding("ctrl+u", "question", "回答", priority=True),
    Binding("ctrl+x", "cancel_turn", "取消任务", priority=True),
    Binding("ctrl+s", "steer_turn", "补充说明"),
    Binding("ctrl+f", "wrap_up_turn", "让它收尾", priority=True),
    Binding("ctrl+r", "reconnect", "重新连接"),
    Binding("ctrl+n", "new_thread", "新建会话"),
    Binding("ctrl+e", "toggle_auto_accept", "自动接受修改", priority=True),
    Binding("f1", "error_help", "帮助"),
    Binding("ctrl+q", "quit_product", "退出"),
]
_MAIN_SCREEN_ACTIONS = frozenset(
    {"approval", "toggle_auto_accept", "question", "cancel_turn", "steer_turn", "wrap_up_turn"}
)
# 只在可用时显示和响应的操作，底部快捷键栏随状态变化。
_CONTEXTUAL_ACTIONS = frozenset(
    {"approval", "question", "cancel_turn", "steer_turn", "wrap_up_turn", "reconnect"}
)


def available_actions(state: ProductControllerState) -> frozenset[str]:
    """当前状态下可用的情境操作。"""

    view = state.thread_view
    try:
        approval, question = pending_approval(view), pending_question(view)
        control = active_turn_control(view)
    except ProductUIError:
        # 交互状态异常时保留入口，打开后显示具体错误。
        return _CONTEXTUAL_ACTIONS
    available = {
        name
        for name, present in (
            ("approval", approval is not None),
            ("question", question is not None),
            ("cancel_turn", control is not None and control.can_cancel),
            ("steer_turn", control is not None and control.can_steer),
            ("wrap_up_turn", control is not None and control.can_steer),
            ("reconnect", state.phase is ControllerPhase.BROKEN),
        )
        if present
    }
    return frozenset(available)


def action_enabled(action: str, state: ProductControllerState, *, modal_open: bool) -> bool:
    """弹窗打开时停用主界面操作；情境操作只在可用时显示和响应。"""

    if action in _MAIN_SCREEN_ACTIONS and modal_open:
        return False
    return action not in _CONTEXTUAL_ACTIONS or action in available_actions(state)


class ProductApp(TurnPacing, App[CloseReport]):
    """Harnessix Code首个可恢复全屏终端入口。"""

    TITLE, SUB_TITLE = "Harnessix Code", "生产级 Coding Agent"
    BINDINGS = _BINDINGS
    ENABLE_COMMAND_PALETTE = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return action_enabled(action, self.controller.state, modal_open=len(self.screen_stack) > 1)

    def __init__(self, controller: ProductController, request: StartRequest) -> None:
        super().__init__()
        self.controller = controller
        self.request = request
        self._main_view = ProductMainView()
        self._presenter = InteractionPresenter(self, controller)
        self._watch_task: asyncio.Task[None] | None = None
        self._intent_in_flight = False
        # 不能叫_closing：那是Textual消息泵的内部属性，设为True后应用丢弃全部消息，退出时卡死。
        self._quitting = False
        self._last_error_code: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield self._main_view
        yield Footer()

    async def on_mount(self) -> None:
        self._watch_task = asyncio.create_task(
            self._watch_controller(), name="harnessix-product-view-updates"
        )
        try:
            state = await self.controller.start(self.request)
        except ProductUIError:
            state = self.controller.state
        await self._render_state(state)

    async def _watch_controller(self) -> None:
        try:
            while True:
                self.post_message(ControllerUpdated(await self.controller.next_update()))
        except asyncio.CancelledError:
            raise

    @on(ControllerUpdated)
    async def _controller_updated(self, message: ControllerUpdated) -> None:
        if self.is_running:
            await self._render_state(message.state)

    async def _render_state(self, state: ProductControllerState) -> None:
        if await self._main_view.render_state(state):
            self._update_composer(state)
            self.refresh_bindings()
            self._presenter.prompt_later(
                lambda: not (self._intent_in_flight or self._quitting or len(self.screen_stack) > 1)
            )

    def _update_composer(self, state: ProductControllerState) -> None:
        self._main_view.update_composer(
            state,
            intent_in_flight=self._intent_in_flight,
            closing=self._quitting,
        )

    async def _dispatch(self, intent: ProductIntent) -> None:
        if self._intent_in_flight or self._quitting:
            return
        self._intent_in_flight = True
        self.query_one("#composer", Input).disabled = True
        try:
            await self.controller.dispatch(intent)
        except ProductUIError as error:
            self._show_error(error)
        finally:
            self._intent_in_flight = False
            state = self.controller.state
            await self._render_state(state)
            # Controller快照可能已由watcher先渲染；revision未变化时仍需根据
            # 本地in-flight门闩重算输入状态，避免Composer保持禁用。
            self._update_composer(state)

    def _show_error(self, error: ProductUIError) -> None:
        self._last_error_code = error.code
        self._main_view.show_error(error)

    async def _run_interaction(self, operation: Callable[[], Awaitable[None]]) -> None:
        if self._intent_in_flight or self._quitting:
            return
        self._intent_in_flight = True
        self._update_composer(self.controller.state)
        try:
            await operation()
        except ProductUIError as error:
            self._show_error(error)
        finally:
            self._intent_in_flight = False
            await self._render_state(self.controller.state)
            self._update_composer(self.controller.state)

    @on(Input.Submitted, "#composer")
    async def _submit_prompt(self, event: Input.Submitted) -> None:
        if self._intent_in_flight or not (text := submitted_text(event)).strip():
            return
        clear_input(event.input)
        self.run_worker(
            self._dispatch(SubmitPromptIntent(text)),
            name="submit-prompt",
            group="local-intent-waiter",
        )

    @on(ListView.Selected, "#threads")
    async def _select_thread(self, event: ListView.Selected) -> None:
        if self._intent_in_flight or event.item.id is None:
            return
        prefix = "thread-"
        if not event.item.id.startswith(prefix):
            return
        self.run_worker(
            self._dispatch(SelectThreadIntent(UUID(hex=event.item.id[len(prefix) :]))),
            name="select-thread",
            group="local-intent-waiter",
        )

    def action_new_thread(self) -> None:
        self.run_worker(
            self._dispatch(CreateThreadIntent()),
            name="new-thread",
            group="local-intent-waiter",
        )

    def action_reconnect(self) -> None:
        self.run_worker(
            self._dispatch(ReconnectIntent()),
            name="reconnect",
            group="local-intent-waiter",
        )

    def _start_interaction(self, operation: Callable[[], Awaitable[None]], name: str) -> None:
        self.run_worker(
            self._run_interaction(operation),
            name=name,
            group="product-interaction",
            exit_on_error=False,
        )

    def action_approval(self) -> None:
        self._start_interaction(self._presenter.approval, "approval")

    def action_toggle_auto_accept(self) -> None:
        self._start_interaction(self._presenter.toggle_auto_accept, "toggle-auto-accept")

    def action_question(self) -> None:
        self._start_interaction(self._presenter.question, "question")

    def action_cancel_turn(self) -> None:
        self._start_interaction(self._presenter.cancel, "cancel-turn")

    def action_steer_turn(self) -> None:
        self._start_interaction(self._presenter.steer, "steer-turn")

    def action_error_help(self) -> None:
        self._start_interaction(
            lambda: self._presenter.help(self._last_error_code),
            "error-help",
        )

    def action_quit_product(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.run_worker(
            self._close_and_exit(),
            name="close-product",
            group="product-lifecycle",
            exit_on_error=False,
        )

    async def _close_and_exit(self) -> None:
        report = await self.controller.close(deadline_seconds=PRODUCT_QUIT_DEADLINE_SECONDS)
        self.exit(report)

    async def on_unmount(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
        if self.controller.state.phase is not ControllerPhase.CLOSED:
            await self.controller.close(deadline_seconds=PRODUCT_QUIT_DEADLINE_SECONDS)
