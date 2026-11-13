"""任务节奏的两项界面操作：一键收尾（Ctrl+F）与运行时间检查点（ADR 0148）。

以Mixin的形式并入ProductApp：自己的定时器、动作和提交监听各自独立，主应用类不为此增长。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import TYPE_CHECKING, Any

from textual.app import App

from harnessix.product_config.wrap_up import WRAP_UP_STEER
from harnessix.product_ui.controller import ProductController, SubmitPromptIntent
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_screens import CheckpointScreen
from harnessix.product_ui.interactions import (
    ACTIVE_TURN_STATES,
    CancelTurnIntent,
    SteerTurnIntent,
    active_turn_control,
)
from harnessix.product_ui.turn_checkpoint import (
    CONTINUE_PROMPT,
    UNANSWERED_PAUSE_SECONDS,
    WRAP_UP_PROMPT,
    ActiveTimeMeter,
    checkpoint_minutes,
)

CHECKPOINT_TICK_SECONDS = 1.0
# 一键收尾替用户发送的补充说明：任务其实已经做完、模型还在自己加验证时用。
# 宿主据此不再执行尚未开始的调用，之后的步骤不提供工具（Beta Issue #4）。
WRAP_UP_MESSAGE = WRAP_UP_STEER


async def wrap_up(controller: ProductController) -> None:
    """替用户发送固定的收尾说明：模型在下一步看到它，停止追加工作并给出总结。"""
    control = active_turn_control(controller.state.thread_view)
    if control is None or not control.can_steer:
        raise ProductUIError("turn_control_stale", "当前Turn已经不可补充输入")
    await controller.dispatch(SteerTurnIntent(control.binding, WRAP_UP_MESSAGE))


def _turn_is_active(controller: ProductController) -> bool:
    control = active_turn_control(controller.state.thread_view)
    return control is not None and control.status in ACTIVE_TURN_STATES


async def _until_idle(controller: ProductController, timeout_seconds: float = 15.0) -> None:
    try:
        async with asyncio.timeout(timeout_seconds):
            while _turn_is_active(controller):  # noqa: ASYNC110
                await asyncio.sleep(0.1)
    except TimeoutError:
        raise ProductUIError("turn_control_stale", "上一轮还没有停下来，请稍后再试") from None


async def run_checkpoint(
    app: App[Any], controller: ProductController, meter: ActiveTimeMeter, pause_after: float
) -> None:
    """先问用户；无人回应则暂停本轮（取消当前Turn）。选择之后按当时的情况执行。"""

    failures: list[ProductUIError] = []

    async def pause() -> bool:
        control = active_turn_control(controller.state.thread_view)
        if control is None or not control.can_cancel:
            return False
        try:
            await controller.dispatch(CancelTurnIntent(control.binding))
        except ProductUIError as error:
            failures.append(error)  # 定时器回调里不能抛出：关闭询问后再显示给用户。
            return False
        return True

    screen = CheckpointScreen(checkpoint_minutes(meter.active), pause_after, pause)
    choice = await app.push_screen_wait(screen)
    meter.restart()
    if failures:
        raise failures[0]
    if screen.paused:
        # 本轮已经暂停：等取消落定后，继续与收尾都从新的一轮开始；停止则就停在这里。
        await _until_idle(controller)
        prompt = {"continue": CONTINUE_PROMPT, "wrap_up": WRAP_UP_PROMPT}.get(choice)
        if prompt is not None:
            await controller.dispatch(SubmitPromptIntent(prompt))
        return
    control = active_turn_control(controller.state.thread_view)
    if control is None or not _turn_is_active(controller):
        return  # 询问期间任务自己结束了。
    if choice == "wrap_up" and control.can_steer:
        await controller.dispatch(SteerTurnIntent(control.binding, WRAP_UP_MESSAGE))
    elif choice == "stop" and control.can_cancel:
        await controller.dispatch(CancelTurnIntent(control.binding))


class TurnPacing:
    """并入ProductApp的任务节奏操作；依赖主应用提供的Controller、交互门闩与交互调度。"""

    if TYPE_CHECKING:
        controller: ProductController
        _intent_in_flight: bool
        _quitting: bool
        screen_stack: list[Any]
        set_interval: Callable[..., Any]
        _start_interaction: Callable[[Callable[[], Awaitable[None]], str], None]

    _pacing_meter: ActiveTimeMeter | None = None
    checkpoint_pause_seconds: float = UNANSWERED_PAUSE_SECONDS

    @property
    def pacing_meter(self) -> ActiveTimeMeter:
        if self._pacing_meter is None:
            self._pacing_meter = ActiveTimeMeter()
        return self._pacing_meter

    async def on_mount(self) -> None:
        # Textual按继承链逐个调用同名处理器：这里只负责启动自己的定时器。
        self.set_interval(CHECKPOINT_TICK_SECONDS, self._tick_checkpoint)

    def _tick_checkpoint(self) -> None:
        """定时累计实际运行时间；到点且界面空闲时打开询问。"""
        if not self.screen_stack:
            return  # 应用正在关闭：界面已全部移除，定时器停下之前还可能再触发一次。
        view = self.controller.state.thread_view
        turn = None if view is None else view.current_turn
        meter = self.pacing_meter
        turn_id, status = (None, None) if turn is None else (turn.turn_id, turn.status)
        meter.observe(turn_id, status, monotonic())
        running, top = _turn_is_active(self.controller), self.screen_stack[-1]
        if isinstance(top, CheckpointScreen):
            if not (running or top.paused):
                top.answer("continue")  # 询问期间任务自己结束了。
            return
        idle = not (self._intent_in_flight or self._quitting or len(self.screen_stack) > 1)
        if meter.due and idle and running:
            self._start_interaction(
                lambda: run_checkpoint(
                    self,  # type: ignore[arg-type]
                    self.controller,
                    meter,
                    self.checkpoint_pause_seconds,
                ),
                "time-checkpoint",
            )

    def action_wrap_up_turn(self) -> None:
        self._start_interaction(lambda: wrap_up(self.controller), "wrap-up-turn")
