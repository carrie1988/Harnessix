"""Approval、Question、Steer与错误帮助的专用Textual Modal。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static

from harnessix.product_ui.approval_automation import EDIT_APPROVAL_TYPES
from harnessix.product_ui.diff_view import readable_diff
from harnessix.product_ui.error_help import ProductErrorHelp
from harnessix.product_ui.interactions import (
    ActiveTurnControl,
    ApprovalEvidenceStatus,
    ApprovalReview,
    PendingApproval,
    PendingQuestion,
)
from harnessix.product_ui.tool_summary import describe_tool

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
.interaction-detail { height: auto; color: $text-muted; }
.interaction-buttons { height: auto; align-horizontal: right; margin-top: 1; }
.interaction-buttons Button { margin-left: 1; }
"""


def approval_summary(pending: PendingApproval) -> str:
    """审批窗口开头的一句话：Agent要做什么；会删除或覆盖数据时额外提醒。"""

    try:
        arguments = json.loads(pending.arguments_json)
    except ValueError:
        arguments = None
    action = (
        describe_tool(pending.tool, arguments) if isinstance(arguments, dict) else None
    ) or f"调用 {pending.tool}"
    if pending.effect_class == "destructive":
        return f"Agent 请求：{action}\n注意：此操作可能删除或覆盖数据，请仔细核对。"
    return f"Agent 请求：{action}"


@dataclass(frozen=True, slots=True)
class ApprovalScreenResult:
    outcome: Literal["approved", "rejected"]
    reason: str | None = None
    remember: bool = False


class ApprovalScreen(ModalScreen[ApprovalScreenResult | None]):
    """展示完整公开证据；关闭窗口不产生审批结果。"""

    CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "close_modal", "关闭")]

    def __init__(self, review: ApprovalReview) -> None:
        super().__init__()
        self.review = review

    def compose(self) -> ComposeResult:
        pending = self.review.pending
        # 审计细节保留一行弱化显示，便于反馈问题时核对。
        detail = (
            f"{pending.tool}@{pending.tool_version} · {pending.policy_version} · "
            f"指纹 {pending.binding.fingerprint[:12]}…"
        )
        with Container(classes="interaction-dialog"):
            yield Label("审批确认", classes="interaction-title")
            yield Static(approval_summary(pending), markup=False, classes="interaction-summary")
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
                yield Button(
                    (
                        "批准并自动接受本会话的修改"
                        if pending.approval_type in EDIT_APPROVAL_TYPES
                        else "批准并在本会话允许相同操作"
                    ),
                    id="approval-approve-session",
                    disabled=not self.review.approve_allowed,
                )
                yield Button("拒绝", id="approval-reject", variant="error")
                yield Button("暂不处理", id="approval-close")
            yield Static("Enter 确认所选按钮 · Tab 切换 · Esc 关闭", classes="interaction-summary")
            yield Static(detail, markup=False, classes="interaction-detail")

    def on_mount(self) -> None:
        pending = self.review.pending
        evidence = self.review.evidence
        log = self.query_one("#approval-evidence", RichLog)
        button = "#approval-approve" if self.review.approve_allowed else "#approval-reject"
        self.query_one(button, Button).focus()
        rendered = (
            readable_diff(evidence.text)
            if evidence.status is ApprovalEvidenceStatus.READY and evidence.text
            else None
        )
        if rendered is not None and pending.approval_type in EDIT_APPROVAL_TYPES:
            # 修改文件只看差异；原始参数与差异重复，且转义后难以核对。
            log.write("将要做的修改：")
            for line in rendered:
                log.write(line)
            return
        log.write("完整参数：")
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
        if event.button.id == "approval-approve-session" and self.review.approve_allowed:
            self.dismiss(ApprovalScreenResult("approved", reason, remember=True))

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


USAGE_HELP = "\n\n".join(
    (
        "开始任务：在底部输入框写下要做的事，按 Enter 发送。",
        # 中文段落里不放空格：终端按空格折行，长中文串会整体挪到下一行。
        "审批：Agent要修改文件或在沙箱外运行命令时，会自动弹出审批窗口。看过差异后选择批准或拒绝；"
        "拒绝时写明原因，Agent会据此调整。关掉的审批可按Ctrl+A再打开。",
        "任务运行中：Ctrl+S 补充说明，Ctrl+X 取消任务。",
        "会话：Ctrl+N 新建会话，在左侧列表切换。下次在同一目录打开，会回到上次的会话。",
        "自动接受修改：Ctrl+E 开关。开启后，本会话的文件修改不再逐个审批，命令仍按规则审批。",
        "退出：Ctrl+Q。遇到错误时按 F1，可查看原因和处理办法。",
        "完整说明见用户手册 docs/operations/user-manual.md。",
    )
)


class HelpScreen(ModalScreen[None]):
    """只显示静态脱敏帮助，不接受原始异常正文；没有错误时显示使用说明。"""

    CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "close_modal", "关闭")]

    def __init__(self, help_item: ProductErrorHelp | None) -> None:
        super().__init__()
        self.help_item = help_item

    def compose(self) -> ComposeResult:
        item = self.help_item
        title, content = (
            ("使用帮助", USAGE_HELP)
            if item is None
            else (
                item.title,
                f"错误分类：{item.code}\n\n"
                f"常见原因：{item.cause}\n\n"
                f"影响：{item.impact}\n\n"
                f"处理动作：{item.action}\n\n"
                f"文档：{item.doc_anchor}",
            )
        )
        with Container(classes="interaction-dialog"):
            yield Label(title, classes="interaction-title")
            yield Static(content, markup=False, classes="interaction-content")
            with Horizontal(classes="interaction-buttons"):
                yield Button("关闭", id="help-close", variant="primary")

    @on(Button.Pressed, "#help-close")
    def _close_pressed(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)
