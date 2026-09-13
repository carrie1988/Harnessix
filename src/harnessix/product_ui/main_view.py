"""Textual主视图：渲染不可变产品快照，不执行领域操作。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Label, ListItem, ListView, RichLog, Static

from harnessix.product_ui.controller import ControllerPhase, ProductControllerState
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interactions import (
    ACTIVE_TURN_STATES,
    pending_approval,
    pending_question,
    usage_cost_view,
)
from harnessix.product_ui.rendering import thread_label, transcript_lines


def _interaction_label(state: ProductControllerState) -> str:
    try:
        if pending_approval(state.thread_view) is not None:
            return " · 待审批 Ctrl+A"
        if pending_question(state.thread_view) is not None:
            return " · 待回答 Ctrl+U"
    except ProductUIError:
        return " · 交互状态异常 F1"
    return ""


def _status_text(state: ProductControllerState) -> str:
    turn = state.thread_view.current_turn if state.thread_view is not None else None
    turn_status = "无活动Turn" if turn is None else f"Turn {turn.status}"
    usage = usage_cost_view(state.thread_view)
    usage_text = ""
    if usage is not None:
        limit = "?" if usage.max_tokens is None else str(usage.max_tokens)
        usage_text = f" · Tokens {usage.total_tokens}/{limit} · 费用未知（{usage.cost_reason}）"
    notice = (
        ""
        if state.last_notice is None
        else f" · {state.last_notice.code}：{state.last_notice.message}"
    )
    return (
        f"{state.phase.value} · 连接代际 {state.connection_generation} · {turn_status}"
        f"{usage_text}{_interaction_label(state)}{notice}"
    )


class ProductMainView(Widget):
    """拥有基础Widget及其已渲染Revision，协议与Controller对其不可见。"""

    DEFAULT_CSS = """
    ProductMainView { height: 1fr; layout: vertical; }
    #workspace { height: 1fr; }
    #threads { width: 28; min-width: 20; border-right: solid $primary; }
    #transcript { width: 1fr; padding: 0 1; }
    #status { height: auto; min-height: 1; padding: 0 1; color: $text-muted; }
    #composer { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._rendered_revision = -1
        self._threads = ListView(id="threads")
        self._transcript = RichLog(id="transcript", wrap=True, markup=False, max_lines=10_000)
        self._status = Static("正在启动…", id="status")
        self._composer = Input(
            placeholder="输入任务，Enter发送",
            id="composer",
            max_length=1_000_000,
            disabled=True,
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace"):
            yield self._threads
            yield self._transcript
        yield self._status
        yield self._composer

    async def render_state(self, state: ProductControllerState) -> bool:
        if not self.is_attached:
            return False
        if state.revision <= self._rendered_revision:
            return False
        self._rendered_revision = state.revision
        await self._threads.clear()
        if not self.is_attached:
            return False
        await self._threads.extend(
            ListItem(Label(thread_label(thread), markup=False), id=f"thread-{thread.thread_id.hex}")
            for thread in state.threads
        )
        if not self.is_attached:
            return False
        if state.selected_thread_id is not None:
            self._threads.index = next(
                (
                    index
                    for index, thread in enumerate(state.threads)
                    if thread.thread_id == state.selected_thread_id
                ),
                None,
            )
        self._transcript.clear()
        lines = transcript_lines(state.thread_view)
        if not lines:
            self._transcript.write("暂无会话。按 Ctrl+N 新建会话。")
        else:
            for line in lines:
                self._transcript.write(f"{line.role}：{line.text}")
        if not self.is_attached:
            return False
        self._status.update(_status_text(state))
        return True

    def update_composer(
        self,
        state: ProductControllerState,
        *,
        intent_in_flight: bool,
        closing: bool,
    ) -> None:
        if not self.is_attached:
            return
        turn = state.thread_view.current_turn if state.thread_view is not None else None
        self._composer.disabled = (
            state.phase is not ControllerPhase.READY
            or state.selected_thread_id is None
            or (turn is not None and turn.status in ACTIVE_TURN_STATES)
            or intent_in_flight
            or closing
        )

    def show_error(self, error: ProductUIError) -> None:
        if self.is_attached:
            self._status.update(f"{error.code}：{error.message} · F1查看帮助")
