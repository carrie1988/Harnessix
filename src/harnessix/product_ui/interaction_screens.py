"""Approval、Question、Steer与错误帮助的专用Textual Modal。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static

from harnessix.product_ui.error_help import ProductErrorHelp
from harnessix.product_ui.interactions import (
    ActiveTurnControl,
    ApprovalEvidenceStatus,
    ApprovalReview,
    PendingQuestion,
)

_MODAL_CSS = """
ModalScreen { align: center middle; }
.interaction-dialog {
    width: 88%; height: 85%; max-width: 120; padding: 1 2;
    border: round $primary; background: $surface;
}
.interaction-title { text-style: bold; height: auto; margin-bottom: 1; }
.interaction-summary { height: auto; margin-bottom: 1; }
.interaction-content { height: 1fr; border: solid $secondary; padding: 0 1; }
.interaction-input { height: auto; margin-top: 1; }
.interaction-error { height: auto; color: $error; }
.interaction-buttons { height: auto; align-horizontal: right; margin-top: 1; }
.interaction-buttons Button { margin-left: 1; }
"""


@dataclass(frozen=True, slots=True)
class ApprovalScreenResult:
    outcome: Literal["approved", "rejected"]
    reason: str | None = None


class ApprovalScreen(ModalScreen[ApprovalScreenResult | None]):
    """展示完整公开证据；关闭窗口不产生审批结果。"""

    CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "close_modal", "关闭")]

    def __init__(self, review: ApprovalReview) -> None:
        super().__init__()
        self.review = review

    def compose(self) -> ComposeResult:
        pending = self.review.pending
        summary = (
            f"类型：{pending.approval_type}  工具：{pending.tool}@{pending.tool_version}\n"
            f"效果：{pending.effect_class}  策略：{pending.policy_version}\n"
            f"指纹：{pending.binding.fingerprint[:12]}…  "
            f"证据：{self.review.evidence.status.value}"
        )
        with Container(classes="interaction-dialog"):
            yield Label("审批确认", classes="interaction-title")
            yield Static(summary, markup=False, classes="interaction-summary")
            yield RichLog(
                id="approval-evidence",
                classes="interaction-content",
                wrap=True,
                markup=False,
                max_lines=20_000,
            )
            yield Input(
                placeholder="可选：填写决定原因",
                id="approval-reason",
                classes="interaction-input",
                max_length=2000,
            )
            yield Static("", id="approval-error", classes="interaction-error", markup=False)
            with Horizontal(classes="interaction-buttons"):
                yield Button(
                    "批准", id="approval-approve", disabled=not self.review.approve_allowed
                )
                yield Button("拒绝", id="approval-reject", variant="error")
                yield Button("暂不处理", id="approval-close")

    def on_mount(self) -> None:
        pending = self.review.pending
        evidence = self.review.evidence
        log = self.query_one("#approval-evidence", RichLog)
        log.write("公开Tool参数：")
        log.write(pending.arguments_json)
        if evidence.status is ApprovalEvidenceStatus.READY:
            log.write("\n完整Diff Artifact：")
            log.write(evidence.text)
        elif evidence.status is ApprovalEvidenceStatus.INLINE:
            log.write("\n单文件Patch以以上公开精确编辑参数作为审批证据。")
        elif evidence.status is ApprovalEvidenceStatus.NOT_REQUIRED:
            log.write("\n该操作不要求额外Diff证据。")
        elif evidence.status is ApprovalEvidenceStatus.REQUIRED:
            log.write("\nDiff尚未读取，批准操作保持禁用。")
        else:
            log.write(f"\nDiff不可用：{evidence.error_code or 'diff_unavailable'}")

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approval-close":
            self.dismiss(None)
            return
        reason = self.query_one("#approval-reason", Input).value.strip() or None
        if event.button.id == "approval-reject":
            self.dismiss(ApprovalScreenResult("rejected", reason))
            return
        if event.button.id == "approval-approve" and self.review.approve_allowed:
            self.dismiss(ApprovalScreenResult("approved", reason))

    def action_close_modal(self) -> None:
        self.dismiss(None)


class QuestionScreen(ModalScreen[str | None]):
    """把选项序号转换为明确答案；Escape不伪造拒绝结果。"""

    CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "close_modal", "关闭")]

    def __init__(self, question: PendingQuestion) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        options = "无预设选项"
        if self.question.options:
            options = "\n".join(
                f"{index}. {value}" for index, value in enumerate(self.question.options, 1)
            )
        with Container(classes="interaction-dialog"):
            yield Label("需要你的输入", classes="interaction-title")
            yield Static(self.question.question, markup=False, classes="interaction-summary")
            yield Static(options, markup=False, classes="interaction-content")
            yield Input(
                placeholder="输入回答或选项序号",
                id="question-answer",
                classes="interaction-input",
                max_length=4000,
            )
            yield Static("", id="question-error", classes="interaction-error", markup=False)
            with Horizontal(classes="interaction-buttons"):
                yield Button("提交回答", id="question-submit", variant="primary")
                yield Button("暂不回答", id="question-close")

    def _submit(self) -> None:
        answer = self.query_one("#question-answer", Input).value.strip()
        if answer.isdecimal() and self.question.options:
            index = int(answer)
            if 1 <= index <= len(self.question.options):
                answer = self.question.options[index - 1]
        if not answer:
            self.query_one("#question-error", Static).update("回答不能为空")
            return
        self.dismiss(answer)

    @on(Input.Submitted, "#question-answer")
    def _answer_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "question-submit":
            self._submit()
        elif event.button.id == "question-close":
            self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


class SteerScreen(ModalScreen[str | None]):
    """收集当前Turn补充输入，不承担Cancel或退出语义。"""

    CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "close_modal", "关闭")]

    def __init__(self, control: ActiveTurnControl) -> None:
        super().__init__()
        self.control = control

    def compose(self) -> ComposeResult:
        with Container(classes="interaction-dialog"):
            yield Label("补充当前Turn", classes="interaction-title")
            yield Static(
                f"当前状态：{self.control.status}",
                markup=False,
                classes="interaction-summary",
            )
            yield Input(
                placeholder="输入需要追加给当前Turn的要求",
                id="steer-text",
                classes="interaction-input",
                max_length=1_000_000,
            )
            yield Static("", id="steer-error", classes="interaction-error", markup=False)
            with Horizontal(classes="interaction-buttons"):
                yield Button("发送补充", id="steer-submit", variant="primary")
                yield Button("关闭", id="steer-close")

    def _submit(self) -> None:
        text = self.query_one("#steer-text", Input).value
        if not text.strip():
            self.query_one("#steer-error", Static).update("补充输入不能为空")
            return
        self.dismiss(text)

    @on(Input.Submitted, "#steer-text")
    def _text_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "steer-submit":
            self._submit()
        elif event.button.id == "steer-close":
            self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """只显示静态脱敏帮助，不接受原始异常正文。"""

    CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "close_modal", "关闭")]

    def __init__(self, help_item: ProductErrorHelp) -> None:
        super().__init__()
        self.help_item = help_item

    def compose(self) -> ComposeResult:
        item = self.help_item
        content = (
            f"错误分类：{item.code}\n\n"
            f"常见原因：{item.cause}\n\n"
            f"影响：{item.impact}\n\n"
            f"处理动作：{item.action}\n\n"
            f"文档：{item.doc_anchor}"
        )
        with Container(classes="interaction-dialog"):
            yield Label(item.title, classes="interaction-title")
            yield Static(content, markup=False, classes="interaction-content")
            with Horizontal(classes="interaction-buttons"):
                yield Button("关闭", id="help-close", variant="primary")

    @on(Button.Pressed, "#help-close")
    def _close_pressed(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)
