"""Textual交互编排：Modal只返回值，领域决定只通过Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from textual.app import App
from textual.timer import Timer

from harnessix.product_ui.controller import ProductController, ToggleAutoAcceptIntent
from harnessix.product_ui.error_help import current_error_help, product_error_help
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_screens import (
    ApprovalScreen,
    HelpScreen,
    QuestionScreen,
    SteerScreen,
)
from harnessix.product_ui.interactions import (
    ApprovalBinding,
    CancelTurnIntent,
    LoadApprovalEvidenceIntent,
    QuestionBinding,
    RespondApprovalIntent,
    RespondQuestionIntent,
    SteerTurnIntent,
    active_turn_control,
    approval_review,
    pending_approval,
    pending_question,
)

PromptKind = Literal["approval", "question"]
# 审批出现后等待片刻再弹窗：沙箱策略或“自动接受修改”通常在这之前就已决定，避免弹窗一闪而过。
AUTO_PROMPT_DELAY_SECONDS = 2.0


class InteractionPresenter:
    """读取Controller快照并驱动一次性Modal，不持有协议或持久化端口。"""

    def __init__(self, app: App[Any], controller: ProductController) -> None:
        self._app = app
        self._controller = controller
        # 已经弹出过（自动或用户手动打开）的审批与提问；每项只自动弹出一次。
        self._prompted: set[ApprovalBinding | QuestionBinding] = set()
        self._prompt_timer: Timer | None = None

    def _pending(self) -> tuple[PromptKind, ApprovalBinding | QuestionBinding] | None:
        view = self._controller.state.thread_view
        try:
            approval, question = pending_approval(view), pending_question(view)
        except ProductUIError:
            return None
        if approval is not None:
            return "approval", approval.binding
        if question is not None:
            return "question", question.binding
        return None

    def prompt_later(self, can_open: Callable[[], bool]) -> None:
        """有尚未弹出过的审批或提问时稍后自动弹窗；届时仍在等待且界面空闲才打开。"""

        pending = self._pending()
        if self._prompt_timer is None and pending is not None and pending[1] not in self._prompted:
            self._prompt_timer = self._app.set_timer(
                AUTO_PROMPT_DELAY_SECONDS, lambda: self._prompt_now(can_open)
            )

    async def _prompt_now(self, can_open: Callable[[], bool]) -> None:
        self._prompt_timer = None
        pending = self._pending()
        if pending is None or pending[1] in self._prompted or not can_open():
            return
        # 领取后不再自动弹出，选“暂不处理”不会反复打扰。
        self._prompted.add(pending[1])
        await self._app.run_action(pending[0])

    async def approval(self) -> None:
        pending = pending_approval(self._controller.state.thread_view)
        if pending is None:
            raise ProductUIError("approval_stale", "当前没有可处理的审批")
        self._prompted.add(pending.binding)
        await self._controller.dispatch(LoadApprovalEvidenceIntent(pending.binding))
        state = self._controller.state
        review = approval_review(state.thread_view, state.approval_evidence)
        result = await self._app.push_screen_wait(ApprovalScreen(review))
        if result is not None:
            await self._controller.dispatch(
                RespondApprovalIntent(
                    review.pending.binding,
                    outcome=result.outcome,
                    reason=result.reason,
                    remember_for_session=result.remember,
                )
            )

    async def toggle_auto_accept(self) -> None:
        await self._controller.dispatch(ToggleAutoAcceptIntent())

    async def question(self) -> None:
        pending = pending_question(self._controller.state.thread_view)
        if pending is None:
            raise ProductUIError("question_stale", "当前没有可回答的问题")
        self._prompted.add(pending.binding)
        answer = await self._app.push_screen_wait(QuestionScreen(pending))
        if answer is not None:
            await self._controller.dispatch(RespondQuestionIntent(pending.binding, answer))

    async def cancel(self) -> None:
        control = active_turn_control(self._controller.state.thread_view)
        if control is None or not control.can_cancel:
            raise ProductUIError("turn_control_stale", "当前Turn已经不可取消")
        await self._controller.dispatch(CancelTurnIntent(control.binding))

    async def steer(self) -> None:
        control = active_turn_control(self._controller.state.thread_view)
        if control is None or not control.can_steer:
            raise ProductUIError("turn_control_stale", "当前Turn已经不可补充输入")
        text = await self._app.push_screen_wait(SteerScreen(control))
        if text is not None:
            await self._controller.dispatch(SteerTurnIntent(control.binding, text))

    async def help(self, code: str | None = None) -> None:
        """有错误时显示原因和处理办法（含本会话最近一轮的任务失败），否则显示使用说明。"""

        state = self._controller.state
        turn = None if state.thread_view is None else state.thread_view.current_turn
        item = (
            product_error_help(code)
            if code is not None
            else current_error_help(
                None if state.last_notice is None else state.last_notice.code,
                None if turn is None else turn.status,
                None if turn is None or turn.error is None else turn.error.code,
            )
        )
        await self._app.push_screen_wait(HelpScreen(item))
