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


def _status(summary: dict) -> dict:
    """判定字段；运行过的命令列表（commands_after_last_edit）另有用例核对。"""
    return {
        k: v
        for k, v in summary.items()
        if k not in {"commands_after_last_edit", "commands_omitted"}
    }


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
    assert _status(verification_summary(_red_then_fix())) == {"status": "unverified"}
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
    assert "有检查没有通过" in last.text and "python -B -m unittest -v" in last.text


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


def test_a_check_run_outside_the_sandbox_counts_as_a_check() -> None:
    """沙箱不可用（Windows、没有Seatbelt的macOS）时命令走host_command.*，逐条审批后运行。

    0.10.0b27 macOS与Windows正式测试：模型修复后用沙箱外命令运行unittest并通过，
    对话末尾仍提示“没有运行过检查”。
    """
    fix, check = uuid4(), uuid4()
    host_run = ("host_command.host-shell", {"cmd": "python -m unittest"})
    checked = [
        _call(fix, "apply_patch_batch"),
        _result(fix, "succeeded"),
        _call(check, *host_run),
        _result(check, "succeeded"),
        _answer("完成。"),
    ]
    assert _status(verification_summary(checked)) == {
        "status": "passed_after_last_edit",
        "last_check": "python -m unittest",
    }
    assert transcript_lines(_view(checked))[-1].role != "提示"
    failed = [*checked[:3], _result(check, "failed"), _answer("完成。")]
    assert "python -m unittest" in transcript_lines(_view(failed))[-1].text


def test_only_a_finished_turn_is_judged() -> None:
    for status in ("calling_model", "executing_tools", "failed", "cancelled"):
        lines = transcript_lines(_view(_red_then_fix(), status=status))
        assert all("没有运行过测试" not in line.text for line in lines)


VITEST = ("host_command.host-shell", {"cmd": "pnpm vitest run --root pkgs/core"})
CLEANUP = ("host_command.host-shell", {"cmd": "Remove-Item repro.test.ts; Test-Path repro.test.ts"})
OTHER_TEST = ("host_command.host-shell", {"cmd": "pnpm vitest run --root pkgs/core repro"})


def _after_a_fix(*runs: tuple[tuple[str, dict[str, object]], str]) -> list[PublicItem]:
    fix = uuid4()
    items = [_call(fix, "apply_patch_batch"), _result(fix, "succeeded")]
    for (tool, arguments), outcome in runs:
        call = uuid4()
        items += [_call(call, tool, arguments), _result(call, outcome)]
    return [*items, _answer("完成。")]


def test_a_successful_cleanup_does_not_hide_a_failed_test() -> None:
    """Beta Issue #36：失败的测试之后一条成功的清理命令，此前把状态翻成“修改后检查通过”。"""
    items = _after_a_fix((VITEST, "failed"), (CLEANUP, "succeeded"))
    assert _status(verification_summary(items)) == {
        "status": "failed_after_last_edit",
        "last_check": CLEANUP[1]["cmd"],
        "failed_check": VITEST[1]["cmd"],
    }
    last = transcript_lines(_view(items))[-1]
    assert last.role == "提示" and VITEST[1]["cmd"] in last.text
    assert CLEANUP[1]["cmd"] not in last.text


def test_a_failed_check_is_resolved_only_by_the_same_command_passing() -> None:
    rerun = _after_a_fix((VITEST, "failed"), (VITEST, "succeeded"), (CLEANUP, "succeeded"))
    assert _status(verification_summary(rerun)) == {
        "status": "passed_after_last_edit",
        "last_check": CLEANUP[1]["cmd"],
    }
    assert transcript_lines(_view(rerun))[-1].role != "提示"
    # 另一条命令通过不算解决（保守：不再报出假的“通过”）。
    other = _after_a_fix((VITEST, "failed"), (OTHER_TEST, "succeeded"))
    assert verification_summary(other)["failed_check"] == VITEST[1]["cmd"]
    # 同一命令通过之后又失败，以最近一次为准。
    again = _after_a_fix((VITEST, "succeeded"), (VITEST, "failed"), (CLEANUP, "succeeded"))
    assert verification_summary(again)["status"] == "failed_after_last_edit"


def test_the_most_recent_unresolved_failure_is_named() -> None:
    items = _after_a_fix((RUN, "failed"), (VITEST, "failed"), (CLEANUP, "succeeded"))
    assert verification_summary(items)["failed_check"] == VITEST[1]["cmd"]
    resolved = _after_a_fix((RUN, "failed"), (VITEST, "failed"), (VITEST, "succeeded"))
    assert verification_summary(resolved)["failed_check"] == RUN[1]["cmd"]


SEARCH = (
    "host_command.host-shell",
    {
        "cmd": "Select-String -Path test\\specs\\gfm\\*.html"
        ' -Pattern \'<a href="[^"]*"[^>]*><code>\''
        " | Select-Object -First 30 Line"
    },
)
PROBE = (
    "host_command.host-shell",
    {"cmd": "python tmp_probe.py 2>&1 | Select-String -NotMatch plugin"},
)


def test_a_read_only_search_after_the_edit_is_not_a_passing_check() -> None:
    """Beta Issue #36的后续（0.10.0b50 marked）：没修好的任务最后一条命令是Select-String搜索，
    完成证据却是“修改后检查通过”。只读或搜索命令不算检查；修改前这里是passed_after_last_edit。"""
    items = _after_a_fix((SEARCH, "succeeded"))
    summary = verification_summary(items)
    assert summary["status"] == "unverified" and "last_check" not in summary
    assert summary["commands_after_last_edit"] == [
        {"command": SEARCH[1]["cmd"], "passed": True, "counted_as_check": False}
    ]
    assert "只读或搜索命令不算" in transcript_lines(_view(items))[-1].text
    # 搜索不覆盖之前的失败，也不替检查作证。
    failed = verification_summary(_after_a_fix((VITEST, "failed"), (SEARCH, "succeeded")))
    assert failed["status"] == "failed_after_last_edit"
    assert failed["last_check"] == VITEST[1]["cmd"]


def test_commands_that_may_run_project_code_still_count() -> None:
    """判定不了的命令仍算检查：探针脚本、带重定向或串联的命令。"""
    for run in (
        PROBE,
        ("host_command.host-shell", {"cmd": "cat out.txt > copy.txt"}),
        ("host_command.host-shell", {"cmd": "grep x a.txt; pytest -q"}),
        ("host_command.host-shell", {"cmd": "grep x a.txt || pytest -q"}),
        ("exec_command.native", {"cmd": "rg needle src | python check.py"}),
    ):
        summary = verification_summary(_after_a_fix((run, "succeeded")))
        assert summary["status"] == "passed_after_last_edit", run
        assert summary["commands_after_last_edit"][0]["counted_as_check"] is True


def test_the_summary_lists_every_command_after_the_last_edit_in_order() -> None:
    items = _after_a_fix((VITEST, "failed"), (SEARCH, "succeeded"), (CLEANUP, "succeeded"))
    listed = verification_summary(items)["commands_after_last_edit"]
    assert [(entry["command"], entry["passed"]) for entry in listed] == [
        (VITEST[1]["cmd"], False),
        (SEARCH[1]["cmd"], True),
        (CLEANUP[1]["cmd"], True),
    ]
    assert [entry["counted_as_check"] for entry in listed] == [True, False, True]


def test_only_the_most_recent_commands_are_listed(monkeypatch) -> None:
    from harnessix.product_ui import verification

    monkeypatch.setattr(verification, "MAX_LISTED_COMMANDS", 2)
    items = _after_a_fix((RUN, "succeeded"), (VITEST, "succeeded"), (CLEANUP, "succeeded"))
    summary = verification_summary(items)
    assert [e["command"] for e in summary["commands_after_last_edit"]] == [
        VITEST[1]["cmd"],
        CLEANUP[1]["cmd"],
    ]
    assert summary["commands_omitted"] == 1
