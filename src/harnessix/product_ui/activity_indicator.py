"""任务运行时的动态指示：转动的符号与已运行时间，让用户知道Agent仍在工作。

模型思考或整段生成写文件内容时，流中可能几十秒没有可显示的正文（百炼qwen3.8实测13～27秒），
界面此前只有静止的“思考中”，看起来像卡住。指示器只在Agent自己在跑时转动；等待用户审批或回答时
不显示，因为那时是在等人。定时器只刷新这一行，不重绘对话。
"""

from __future__ import annotations

from time import monotonic
from typing import Final
from uuid import UUID

from textual.widgets import Static

from harnessix.product_ui.controller import ControllerPhase, ProductControllerState
from harnessix.product_ui.generation_estimate import InFlightRequest, in_flight_request
from harnessix.product_ui.rendering import turn_status_label

TICK_SECONDS: Final = 0.12
# 模型服务这么久没有返回新内容时，运行提示改为说明等了多久，而不是停在一个不动的数字上。
SILENCE_NOTICE_SECONDS: Final = 5.0
_FRAMES: Final = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_RUNNING: Final = frozenset(
    {
        "accepted",
        "preparing_context",
        "calling_model",
        "executing_tools",
        "waiting_action",
        "finalizing",
        "cancelling",
    }
)


def activity_text(
    status: str,
    elapsed_seconds: float,
    frame: int,
    *,
    streaming: bool,
    generation: InFlightRequest | None = None,
    silent_seconds: float = 0.0,
) -> str:
    """一行运行提示，例如“⠹ 思考中 · 已运行 23 秒”。

    有生成进度时写明模型正在做什么、已经生成了多少：
    “⠹ 正在生成修改内容 · 约 3,400 tokens · 已运行 2 分 05 秒”。
    进度有一阵没有变化时改为说明等了多久：“⠹ 正在生成修改内容 · 模型服务 18 秒未返回新内容 · …”。
    请求发出后还没有收到任何内容（等首帧，或读超时后正在重试）时同样说明：
    “⠹ 等待模型服务响应 · 已等待 3 分 01 秒 · …”。
    silent_seconds从最近一次有新内容、或这一步开始调用模型算起。
    """

    waiting = silent_seconds >= SILENCE_NOTICE_SECONDS
    if status == "calling_model" and generation is None and not streaming and waiting:
        label = f"等待模型服务响应 · 已等待 {_duration(silent_seconds)}"
    elif generation is not None and status == "calling_model":
        label = (
            generation.label
            if not waiting
            else f"{generation.phase_label} · 模型服务 {int(silent_seconds)} 秒未返回新内容"
        )
    else:
        label = (
            "生成回复中" if streaming and status == "calling_model" else turn_status_label(status)
        )
    return f"{_FRAMES[frame % len(_FRAMES)]} {label} · 已运行 {_duration(elapsed_seconds)}"


def _duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole} 秒"
    return f"{whole // 60} 分 {whole % 60:02d} 秒"


class ActivityIndicator(Static):
    """只在Turn运行时显示；计时从界面第一次看到这一轮开始，等待审批或回答的时间也计入。"""

    DEFAULT_CSS = """
    ActivityIndicator { height: 1; padding: 0 1; color: $accent; }
    """

    def __init__(self) -> None:
        super().__init__("", id="activity", markup=False)
        self.display = False
        self._turn: UUID | None = None
        self._since = 0.0
        self._status = ""
        self._streaming = False
        self._generation: InFlightRequest | None = None
        # 最近一次有新内容（生成进度变化），或进入当前状态的时刻；等待提示从这里算起。
        self._generation_at = 0.0
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(TICK_SECONDS, self._tick)

    def follow(self, state: ProductControllerState) -> None:
        """随Controller状态切换显示；同一轮内只更新状态，不重置计时。"""

        view = state.thread_view
        turn = None if view is None else view.current_turn
        if state.phase is not ControllerPhase.READY or turn is None or turn.status not in _RUNNING:
            # 等待用户时只隐藏、保留这一轮的起点：沙箱命令的自动批准也会短暂经过等待审批，
            # 在这里清零会让“已运行”变成“距上一条命令的时间”。
            if turn is None:
                self._turn = None
            self.display = False
            return
        if turn.turn_id != self._turn:
            self._turn, self._since = turn.turn_id, monotonic()
            self._status = ""
        if turn.status != self._status:
            self._generation_at = monotonic()
        self._status = turn.status
        # model_step是已结算用量的步数；正在调用模型的步骤号更大。思考预览不算回复正文。
        self._streaming = view is not None and any(
            stream.text
            and not (stream.reasoning or stream.progress)
            and stream.turn_id == turn.turn_id
            and stream.model_step > turn.model_step
            for stream in view.streams
        )
        generation = in_flight_request(view)
        if generation != self._generation:
            self._generation, self._generation_at = generation, monotonic()
        self.display = True
        self._render_line()

    def _tick(self) -> None:
        if self._turn is None or not self.display:
            return
        self._frame += 1
        self._render_line()

    def _render_line(self) -> None:
        self.update(
            activity_text(
                self._status,
                monotonic() - self._since,
                self._frame,
                streaming=self._streaming,
                generation=self._generation,
                silent_seconds=monotonic() - self._generation_at,
            )
        )
