from __future__ import annotations

from uuid import uuid4

from textual.app import App
from textual.widgets import Button, Input, Static

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


def _approval_review(status: ApprovalEvidenceStatus) -> ApprovalReview:
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
