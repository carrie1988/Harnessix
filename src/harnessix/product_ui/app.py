"""Textual产品壳：只处理布局、输入Intent和不可变状态渲染。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, RichLog, Static

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
from harnessix.product_ui.rendering import thread_label, transcript_lines


class ControllerUpdated(Message):
    """把Controller快照送回Textual消息循环。"""

    def __init__(self, state: ProductControllerState) -> None:
        super().__init__()
        self.state = state


class ProductApp(App[CloseReport]):
    """Harnessix Code首个可恢复全屏终端入口。"""

    TITLE = "Harnessix Code"
    SUB_TITLE = "生产级 Coding Agent"
    BINDINGS = [
        Binding("ctrl+n", "new_thread", "新建会话"),
        Binding("ctrl+r", "reconnect", "重新连接"),
        Binding("ctrl+q", "quit_product", "退出"),
    ]
    CSS = """
    #workspace { height: 1fr; }
    #threads { width: 28; min-width: 20; border-right: solid $primary; }
    #transcript { width: 1fr; padding: 0 1; }
    #status { height: auto; min-height: 1; padding: 0 1; color: $text-muted; }
    #composer { dock: bottom; }
    """

    def __init__(self, controller: ProductController, request: StartRequest) -> None:
        super().__init__()
        self.controller = controller
        self.request = request
        self._watch_task: asyncio.Task[None] | None = None
        self._intent_in_flight = False
        self._closing = False
        self._rendered_revision = -1

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            yield ListView(id="threads")
            yield RichLog(id="transcript", wrap=True, markup=False, max_lines=10_000)
        yield Static("正在启动…", id="status")
        yield Input(
            placeholder="输入任务，Enter发送",
            id="composer",
            max_length=1_000_000,
            disabled=True,
        )
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
        await self._render_state(message.state)

    async def _render_state(self, state: ProductControllerState) -> None:
        if state.revision <= self._rendered_revision:
            return
        self._rendered_revision = state.revision
        threads = self.query_one("#threads", ListView)
        await threads.clear()
        await threads.extend(
            ListItem(Label(thread_label(thread), markup=False), id=f"thread-{thread.thread_id.hex}")
            for thread in state.threads
        )
        if state.selected_thread_id is not None:
            threads.index = next(
                (
                    index
                    for index, thread in enumerate(state.threads)
                    if thread.thread_id == state.selected_thread_id
                ),
                None,
            )

        transcript = self.query_one("#transcript", RichLog)
        transcript.clear()
        lines = transcript_lines(state.thread_view)
        if not lines:
            transcript.write("暂无会话。按 Ctrl+N 新建会话。")
        else:
            for line in lines:
                transcript.write(f"{line.role}：{line.text}")

        turn = state.thread_view.current_turn if state.thread_view is not None else None
        turn_status = "无活动Turn" if turn is None else f"Turn {turn.status}"
        notice = (
            ""
            if state.last_notice is None
            else f" · {state.last_notice.code}：{state.last_notice.message}"
        )
        self.query_one("#status", Static).update(
            f"{state.phase.value} · 连接代际 {state.connection_generation} · {turn_status}{notice}"
        )
        self._update_composer(state)

    def _update_composer(self, state: ProductControllerState) -> None:
        composer = self.query_one("#composer", Input)
        composer.disabled = (
            state.phase is not ControllerPhase.READY
            or state.selected_thread_id is None
            or self._intent_in_flight
        )

    async def _dispatch(self, intent: ProductIntent) -> None:
        if self._intent_in_flight or self._closing:
            return
        self._intent_in_flight = True
        self.query_one("#composer", Input).disabled = True
        try:
            await self.controller.dispatch(intent)
        except ProductUIError as error:
            self.query_one("#status", Static).update(f"{error.code}：{error.message}")
        finally:
            self._intent_in_flight = False
            await self._render_state(self.controller.state)

    @on(Input.Submitted, "#composer")
    async def _submit_prompt(self, event: Input.Submitted) -> None:
        if self._intent_in_flight or not event.value.strip():
            return
        event.input.value = ""
        self.run_worker(
            self._dispatch(SubmitPromptIntent(event.value)),
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

    def action_quit_product(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.run_worker(
            self._close_and_exit(),
            name="close-product",
            group="product-lifecycle",
            exit_on_error=False,
        )

    async def _close_and_exit(self) -> None:
        report = await self.controller.close(deadline_seconds=10)
        self.exit(report)

    async def on_unmount(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
        if self.controller.state.phase is not ControllerPhase.CLOSED:
            await self.controller.close(deadline_seconds=10)
