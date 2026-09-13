"""Textual交互编排：Modal只返回值，领域决定只通过Controller。"""

from __future__ import annotations

from typing import Any

from textual.app import App

from harnessix.product_ui.controller import ProductController
from harnessix.product_ui.error_help import product_error_help
from harnessix.product_ui.errors import ProductUIError
from harnessix.product_ui.interaction_screens import (
    ApprovalScreen,
    HelpScreen,
    QuestionScreen,
    SteerScreen,
)
from harnessix.product_ui.interactions import (
    CancelTurnIntent,
    LoadApprovalEvidenceIntent,
    RespondApprovalIntent,
    RespondQuestionIntent,
    SteerTurnIntent,
    active_turn_control,
    approval_review,
    pending_approval,
    pending_question,
)


class InteractionPresenter:
    """读取Controller快照并驱动一次性Modal，不持有协议或持久化端口。"""

    def __init__(self, app: App[Any], controller: ProductController) -> None:
        self._app = app
        self._controller = controller

    async def approval(self) -> None:
        pending = pending_approval(self._controller.state.thread_view)
        if pending is None:
            raise ProductUIError("approval_stale", "当前没有可处理的审批")
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
                )
            )

    async def question(self) -> None:
        pending = pending_question(self._controller.state.thread_view)
        if pending is None:
            raise ProductUIError("question_stale", "当前没有可回答的问题")
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
        notice = self._controller.state.last_notice
        await self._app.push_screen_wait(
            HelpScreen(product_error_help(code or (None if notice is None else notice.code)))
        )
