"""“已完成”但最后一次修改之后没有运行检查时，对话末尾如实标出（Beta Issue #9）。

0.10.0b25 Windows实测：模型的总结声称“修复后运行 python -B -m unittest -v → Ran 31 tests … OK”，
实际唯一一次测试命令发生在修复补丁之前（预期的红测），之后没有任何检查。`harnessix exec`的摘要里
早有“最后一次修改之后是否运行过检查”这一项，终端界面没有显示它。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from harnessix.product_ui.projection import ProductViewState, ProjectedItem, TurnProjection
from harnessix.product_ui.rendering import transcript_lines
from harnessix.product_ui.verification import verification_summary
from harnessix.protocol.contracts import PublicItem, ThreadView

NOW = datetime(2026, 11, 13, tzinfo=UTC)
RUN = ("exec_command.native", {"cmd": "python -B -m unittest -v"})


def _item(content: dict[str, object]) -> PublicItem:
    return PublicItem.model_validate(
        {"item_id": uuid4(), "status": "completed", "content": content}
    )


def _call(call_id: UUID, tool: str, arguments: dict[str, object] | None = None) -> PublicItem:
    return _item(
        {
            "kind": "tool_call",
            "call_id": call_id,
            "tool": tool,
            "tool_version": "v1",
            "effect_class": "non_idempotent_write",
            "requires_approval": True,
            "arguments": arguments or {},
        }
    )


def _result(call_id: UUID, outcome: str) -> PublicItem:
    return _item({"kind": "tool_result", "call_id": call_id, "outcome": outcome})


def _answer(text: str) -> PublicItem:
    return _item({"kind": "assistant_message", "text": text})


def _view(items: list[PublicItem], status: str = "completed") -> ProductViewState:
    thread = ThreadView(
        thread_id=uuid4(), workspace="/w", cursor=1, turn_count=1, created_at=NOW, updated_at=NOW
    )
    turn = TurnProjection(
        turn_id=uuid4(), request_id="r", status=status, budget=None, usage=None, model_step=3
    )
    projected = tuple(
        ProjectedItem(item=item, turn_id=turn.turn_id, first_cursor=1, last_cursor=1, final=True)
        for item in items
    )
    return ProductViewState(thread=thread, durable_cursor=1, items=projected, current_turn=turn)


def _red_then_fix() -> list[PublicItem]:
    """Issue #9的时间线：先红测（失败），再修复补丁，之后没有任何检查，总结却说测试通过。"""
    red, fix = uuid4(), uuid4()
    return [
        _call(red, *RUN),
        _result(red, "failed"),
        _call(fix, "apply_patch_batch"),
        _result(fix, "succeeded"),
        _answer("已修复。python -B -m unittest -v → Ran 31 tests … OK"),
    ]


def test_a_completed_turn_without_a_check_after_its_last_edit_is_flagged() -> None:
    assert verification_summary(_red_then_fix()) == {"status": "unverified"}
    last = transcript_lines(_view(_red_then_fix()))[-1]
    assert last.role == "提示" and not last.final
    assert last.text.startswith("本轮最后一次修改之后没有运行过测试或其他检查")


def test_a_failed_check_after_the_last_edit_is_flagged_with_the_command() -> None:
    fix, check = uuid4(), uuid4()
    items = [
        _call(fix, "apply_patch_batch"),
        _result(fix, "succeeded"),
        _call(check, *RUN),
        _result(check, "failed"),
        _answer("完成。"),
    ]
    last = transcript_lines(_view(items))[-1]
    assert last.role == "提示"
    assert "最后一次检查没有通过" in last.text and "python -B -m unittest -v" in last.text


def test_nothing_is_added_when_the_last_edit_was_checked_or_nothing_was_edited() -> None:
    fix, check = uuid4(), uuid4()
    checked = [
        _call(fix, "apply_patch_batch"),
        _result(fix, "succeeded"),
        _call(check, *RUN),
        _result(check, "succeeded"),
        _answer("完成。"),
    ]
    assert transcript_lines(_view(checked))[-1].role != "提示"
    assert transcript_lines(_view([_answer("这个函数是做什么的：……")]))[-1].role != "提示"


def test_only_a_finished_turn_is_judged() -> None:
    for status in ("calling_model", "executing_tools", "failed", "cancelled"):
        lines = transcript_lines(_view(_red_then_fix(), status=status))
        assert all("没有运行过测试" not in line.text for line in lines)
