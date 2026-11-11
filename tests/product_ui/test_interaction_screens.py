from __future__ import annotations

from uuid import uuid4

from textual.app import App
from textual.widgets import Button, Input, Label, RichLog, Static

from harnessix.product_ui.error_help import product_error_help
from harnessix.product_ui.interaction_screens import (
    ApprovalScreen,
    ApprovalScreenResult,
    HelpScreen,
    QuestionScreen,
    SteerScreen,
)
from harnessix.product_ui.interactions import (
    ActiveTurnControl,
    ApprovalBinding,
    ApprovalEvidence,
    ApprovalEvidenceStatus,
    ApprovalReview,
    PendingApproval,
    PendingQuestion,
    QuestionBinding,
    TurnBinding,
)


def _approval_review(status: ApprovalEvidenceStatus, text: str = "") -> ApprovalReview:
    binding = ApprovalBinding(uuid4(), uuid4(), uuid4(), uuid4(), "a" * 64)
    pending = PendingApproval(
        binding=binding,
        approval_type="patch_batch",
        policy_version="policy-v1",
        tool="workspace.patch_batch",
        tool_version="1",
        effect_class="idempotent_write",
        arguments_json='{"edits":[]}',
        diff_artifact=None,
    )
    evidence = ApprovalEvidence(
        binding,
        status,
        error_code="diff_unavailable" if status is ApprovalEvidenceStatus.UNAVAILABLE else None,
        text=text,
    )
    return ApprovalReview(
        pending,
        evidence,
        approve_allowed=status
        in {
            ApprovalEvidenceStatus.INLINE,
            ApprovalEvidenceStatus.NOT_REQUIRED,
            ApprovalEvidenceStatus.READY,
        },
    )


async def test_approval_screen_disables_blind_approval_but_allows_rejection() -> None:
    app: App[None] = App()
    results: list[ApprovalScreenResult | None] = []
    async with app.run_test(size=(100, 30)) as pilot:
        screen = ApprovalScreen(_approval_review(ApprovalEvidenceStatus.UNAVAILABLE))
        await app.push_screen(
            screen,
            callback=results.append,
        )
        await pilot.pause()
        assert screen.query_one("#approval-approve", Button).disabled
        assert not screen.query_one("#approval-reject", Button).disabled
        await pilot.click("#approval-reject")
        await pilot.pause()
    assert results == [ApprovalScreenResult("rejected")]


async def test_approval_screen_shows_a_readable_diff_and_enter_approves() -> None:
    from tests.product_ui.test_diff_view import ARTIFACT

    app: App[None] = App()
    results: list[ApprovalScreenResult | None] = []
    async with app.run_test(size=(120, 40)) as pilot:
        screen = ApprovalScreen(_approval_review(ApprovalEvidenceStatus.READY, ARTIFACT))
        await app.push_screen(screen, callback=results.append)
        await pilot.pause()
        log = screen.query_one("#approval-evidence", RichLog)
        shown = "\n".join(line.text for line in log.lines)
        assert "+    return len(text.split())" in shown and "record_type" not in shown
        assert screen.query_one("#approval-approve", Button).has_focus
        await pilot.press("enter")
        await pilot.pause()
    assert results == [ApprovalScreenResult("approved")]


async def test_question_screen_maps_option_number_and_escape_sends_nothing() -> None:
    question = PendingQuestion(
        QuestionBinding(uuid4(), uuid4(), uuid4(), uuid4()),
        "选择环境",
        ("测试", "生产"),
    )
    app: App[None] = App()
    results: list[str | None] = []
    async with app.run_test(size=(80, 24)) as pilot:
        screen = QuestionScreen(question)
        await app.push_screen(screen, callback=results.append)
        answer = screen.query_one("#question-answer", Input)
        answer.value = "2"
        answer.focus()
        await pilot.press("enter")
        await pilot.pause()
        await app.push_screen(QuestionScreen(question), callback=results.append)
        await pilot.press("escape")
        await pilot.pause()
    assert results == ["生产", None]


async def test_steer_screen_rejects_empty_text_and_returns_explicit_text() -> None:
    control = ActiveTurnControl(
        TurnBinding(uuid4(), uuid4()),
        status="calling_model",
        can_cancel=True,
        can_steer=True,
    )
    app: App[None] = App()
    results: list[str | None] = []
    async with app.run_test(size=(80, 24)) as pilot:
        screen = SteerScreen(control)
        await app.push_screen(screen, callback=results.append)
        field = screen.query_one("#steer-text", Input)
        field.focus()
        await pilot.press("enter")
        assert screen.query_one("#steer-error", Static).content == "补充输入不能为空"
        field.value = "先运行测试"
        await pilot.press("enter")
        await pilot.pause()
    assert results == ["先运行测试"]


async def test_help_screen_uses_sanitized_fallback_for_unknown_code() -> None:
    item = product_error_help("provider-secret-/private/path")
    assert item.code == "product_internal_failure"
    assert "provider-secret" not in repr(item)

    app: App[None] = App()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = HelpScreen(item)
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#help-close", Button).label.plain == "关闭"
        await pilot.press("escape")


async def test_help_without_an_error_is_a_usage_guide() -> None:
    from harnessix.product_ui.interaction_screens import USAGE_HELP

    app: App[None] = App()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = HelpScreen(None)
        await app.push_screen(screen)
        await pilot.pause()
        # 没有错误时按F1，此前显示的是“终端产品发生内部错误”的帮助条目。
        title = str(screen.query_one(".interaction-title", Label).render())
        assert title == "使用帮助"
        assert "开始任务" in USAGE_HELP and "内部错误" not in USAGE_HELP


def _three_file_artifact() -> str:
    """三个文件、远超一屏的差异（Beta补测RT-01：128×36终端，首次审批3个文件）。"""
    import json

    paths = ("app/schemas/common.py", "app/api/routes.py", "app/services/locate.py")
    diff = "".join(
        f"diff --harnessix a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,40 +1,40 @@\n"
        + "".join(f"-old line {n} of {path}\n+new line {n} of {path}\n" for n in range(40))
        for path in paths
    )
    records: list[dict[str, object]] = [{"record_type": "summary", "file_count": 3}]
    records += [
        {"record_type": "entry", "index": index, "entry": {"kind": "modified", "path": path}}
        for index, path in enumerate(paths)
    ]
    records.append({"record_type": "text", "sequence": 0, "text": diff})
    return "\n".join(json.dumps(record) for record in records)


async def test_a_long_diff_opens_at_its_start_and_pages_without_leaving_the_buttons() -> None:
    app: App[None] = App()
    async with app.run_test(size=(128, 36)) as pilot:
        screen = ApprovalScreen(
            _approval_review(ApprovalEvidenceStatus.READY, _three_file_artifact())
        )
        await app.push_screen(screen)
        await pilot.pause()
        log = screen.query_one("#approval-evidence", RichLog)
        approve = screen.query_one("#approval-approve", Button)
        assert log.virtual_size.height > log.size.height
        # 打开时停在开头：用户先看到文件清单和第一个文件，而不是最后一个文件的末尾。
        assert log.scroll_y == 0
        first_page = [line.text for line in log.lines[: log.size.height]]
        assert first_page[:5] == [
            "将要做的修改：",
            "共 3 个文件：",
            "  修改  app/schemas/common.py",
            "  修改  app/api/routes.py",
            "  修改  app/services/locate.py",
        ]
        assert approve.has_focus
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0 and approve.has_focus
        await pilot.press("pageup")
        await pilot.pause()
        assert log.scroll_y == 0 and approve.has_focus
        hints = "\n".join(str(widget.render()) for widget in screen.query(Static))
        assert "PgUp/PgDn 翻看" in hints
