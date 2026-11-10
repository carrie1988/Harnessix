"""Textual主视图：渲染不可变产品快照，不执行领域操作。"""

from __future__ import annotations

from typing import Final

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Label, ListItem, ListView, Static

from harnessix.product_ui.controller import ControllerPhase, ProductControllerState
from harnessix.product_ui.cost_estimate import cost_upper_bound
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interactions import (
    ACTIVE_TURN_STATES,
    UsageCostView,
    pending_approval,
    pending_question,
    usage_cost_view,
)
from harnessix.product_ui.rendering import thread_label, transcript_lines, turn_status_label
from harnessix.product_ui.transcript_pane import TranscriptPane
from harnessix.product_ui.turn_budget import UNCAPPED_TOKENS

# 窄于此宽度时收起会话列表，把空间留给对话。
NARROW_WIDTH: Final = 90
_PHASE_LABELS: Final = {
    ControllerPhase.CREATED: "正在启动…",
    ControllerPhase.STARTING: "正在连接…",
    ControllerPhase.BROKEN: "连接中断，按 Ctrl+R 重新连接",
    ControllerPhase.CLOSING: "正在退出…",
    ControllerPhase.CLOSED: "已退出",
}
_STATUS_KEYS: Final = {"waiting_approval": "（Ctrl+A）", "waiting_input": "（Ctrl+U）"}


def _interaction_label(state: ProductControllerState, turn_status: str | None) -> str:
    """Turn状态已提示按键时不再重复。"""

    try:
        if pending_approval(state.thread_view) is not None and turn_status != "waiting_approval":
            return " · 待审批 Ctrl+A"
        if pending_question(state.thread_view) is not None and turn_status != "waiting_input":
            return " · 待回答 Ctrl+U"
    except ProductUIError:
        return " · 交互状态异常 F1"
    return ""


def _activity_text(state: ProductControllerState) -> str:
    if state.phase is not ControllerPhase.READY:
        return _PHASE_LABELS.get(state.phase, state.phase.value)
    turn = state.thread_view.current_turn if state.thread_view is not None else None
    if turn is None:
        return "就绪"
    return turn_status_label(turn.status) + _STATUS_KEYS.get(turn.status, "")


def _cost_text(state: ProductControllerState, usage: UsageCostView) -> str:
    if state.pricing == "free":
        return "本地 0 元"
    if state.list_price is None:
        return f"费用未知（{usage.cost_reason}）"
    return cost_upper_bound(
        state.list_price, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
    ).text()


def _status_text(state: ProductControllerState) -> str:
    turn = state.thread_view.current_turn if state.thread_view is not None else None
    usage = usage_cost_view(state.thread_view)
    usage_text = ""
    if usage is not None:
        tokens = (
            f"{usage.total_tokens:,} tokens"
            if usage.max_tokens is None or usage.max_tokens >= UNCAPPED_TOKENS
            else f"{usage.total_tokens:,}/{usage.max_tokens:,} tokens"
        )
        usage_text = f" · {tokens} · {_cost_text(state, usage)}"
    sandbox = "" if state.sandbox is None else f" · {state.sandbox}"
    if state.auto_accept_edits:
        sandbox += " · 自动接受修改"
    notice = (
        ""
        if state.last_notice is None
        else f" · {state.last_notice.message}（{state.last_notice.code}）"
    )
    interaction = _interaction_label(state, None if turn is None else turn.status)
    return f"{_activity_text(state)}{sandbox}{usage_text}{interaction}{notice}"


class ThreadList(ListView):
    """会话列表，标签为创建时间、状态和轮数。"""

    def __init__(self) -> None:
        super().__init__(id="threads")
        self._labels: tuple[tuple[str, str], ...] = ()

    async def show(self, state: ProductControllerState) -> bool:
        """只在标签变化时重建列表；选中会话用当前Turn的实时状态。"""

        turn = state.thread_view.current_turn if state.thread_view is not None else None
        labels = tuple(
            (
                f"thread-{thread.thread_id.hex}",
                thread_label(
                    thread,
                    turn.status
                    if turn is not None and thread.thread_id == state.selected_thread_id
                    else None,
                ),
            )
            for thread in state.threads
        )
        if labels != self._labels:
            await self.clear()
            if not self.is_attached:
                return False
            await self.extend(
                ListItem(Label(label, markup=False), id=item_id) for item_id, label in labels
            )
            self._labels = labels
        if not self.is_attached:
            return False
        if state.selected_thread_id is not None:
            self.index = next(
                (
                    index
                    for index, thread in enumerate(state.threads)
                    if thread.thread_id == state.selected_thread_id
                ),
                None,
            )
        return True


class ProductMainView(Widget):
    """拥有基础Widget及其已渲染Revision，协议与Controller对其不可见。"""

    DEFAULT_CSS = """
    ProductMainView { height: 1fr; layout: vertical; }
    #workspace { height: 1fr; }
    #threads { width: 28; min-width: 20; border-right: solid $primary; }
    #status { height: auto; min-height: 1; padding: 0 1; color: $text-muted; }
    #composer { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._rendered_revision = -1
        self._threads = ThreadList()
        self._conversation = TranscriptPane()
        self._last_state: ProductControllerState | None = None
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
            yield self._conversation
        yield self._status
        yield self._composer

    async def render_state(self, state: ProductControllerState) -> bool:
        if not self.is_attached:
            return False
        if state.revision <= self._rendered_revision:
            return False
        self._rendered_revision = state.revision
        self._last_state = state
        if not await self._threads.show(state):
            return False
        self._render_transcript(state)
        self._status.update(_status_text(state))
        return True

    def on_resize(self, event: events.Resize) -> None:
        self._threads.display = event.size.width >= NARROW_WIDTH
        # 布局更新后按新宽度重新折行，并回到最新内容。
        self.call_after_refresh(self._rewrap)

    def _rewrap(self) -> None:
        if self._last_state is not None and self.is_attached:
            self._render_transcript(self._last_state, rewrap=True)

    def _render_transcript(self, state: ProductControllerState, *, rewrap: bool = False) -> None:
        lines = transcript_lines(state.thread_view)
        self._conversation.show(lines, state.selected_thread_id, rewrap=rewrap)

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
        was_disabled = self._composer.disabled
        self._composer.disabled = (
            state.phase is not ControllerPhase.READY
            or (turn is not None and turn.status in ACTIVE_TURN_STATES)
            or intent_in_flight
            or closing
        )
        if was_disabled and not self._composer.disabled:
            # 禁用期间焦点会移到会话列表；恢复可输入时交还输入框，新建会话或Turn结束后可直接打字。
            self._composer.focus()

    def show_error(self, error: ProductUIError) -> None:
        if self.is_attached:
            self._status.update(f"{error.code}：{error.message} · F1查看帮助")
