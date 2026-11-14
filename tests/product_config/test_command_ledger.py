"""命令账本：通过后又失败、同一补丁反复回滚、同一命令原地重复，只按持久事实各说明一次。"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import (
    Budget,
    CompactionWindow,
    Item,
    ItemContent,
    ItemStatus,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.step_notices import step_notice_item_id
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor, utc_now
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.command_ledger import (
    REPEATED_ROLLBACK_THRESHOLD,
    REPEATED_RUN_THRESHOLD,
    CommandLedgerNotices,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer

SHELL = "exec_command.sandbox"
CHECK = "cd repo && ./run-tests unit"
SOURCE = "src/reader.py"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _item(content: ItemContent) -> Item:
    return Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)


def _step(tool: str, arguments: dict[str, object], result: dict[str, object]) -> list[Item]:
    """模型的一次响应：一句说明、一个调用及其结果。"""
    call_id = uuid4()
    call = ToolCallContent(
        call_id=call_id,
        provider_call_id="provider",
        tool=tool,
        tool_version="1",
        effect_class=EffectClass.READ_ONLY,
        arguments=arguments,
    )
    return [
        _item(TextContent(kind="assistant_message", text="next")),
        _item(call),
        _item(ToolResultContent(call_id=call_id, **result)),
    ]


def exit_result(
    returncode: int = 0, *, stdout: str = "", stop_reason: str = "exited"
) -> dict[str, object]:
    """命令结果里账本用到的事实：终态、退出码与两路输出的观察摘要。"""
    output = {
        "version": "trusted-process-output/v4",
        "state": "exited",
        "returncode": returncode,
        "stop_reason": stop_reason,
        "stdout": {"observed_sha256": _sha(stdout)},
        "stderr": {"observed_sha256": _sha("")},
    }
    outcome = "succeeded" if returncode == 0 and stop_reason == "exited" else "failed"
    return {"outcome": outcome, "output": output}


def patch_result(
    transaction_id: UUID, *, diff: str = "fix", paths: tuple[str, ...] = (SOURCE,)
) -> dict[str, object]:
    output = {
        "spec_version": "harnessix.workspace-patch-output/v2",
        "transaction_id": str(transaction_id),
        "diff_sha256": _sha(diff),
        "changes": [{"path": path, "content_sha256": _sha(path + diff)} for path in paths],
    }
    return {"outcome": "succeeded", "output": output}


def run(
    command: str = CHECK,
    *,
    returncode: int = 0,
    stdout: str = "",
    tool: str = SHELL,
    stop_reason: str = "exited",
) -> list[Item]:
    result = exit_result(returncode, stdout=stdout, stop_reason=stop_reason)
    return _step(tool, {"cmd": command}, result)


def patch(
    transaction_id: UUID, *, diff: str = "fix", paths: tuple[str, ...] = (SOURCE,)
) -> list[Item]:
    return _step(
        "apply_patch_batch", {"files": []}, patch_result(transaction_id, diff=diff, paths=paths)
    )


def rollback(target: UUID, *, outcome: str = "succeeded") -> list[Item]:
    output = {
        "spec_version": "harnessix.workspace-patch-output/v2",
        "transaction_id": str(uuid4()),
        "diff_sha256": _sha(f"undo {target}"),
        "changes": [{"path": SOURCE, "content_sha256": _sha("before")}],
    }
    return _step(
        "rollback_workspace_patch",
        {"transaction_id": str(target)},
        {"outcome": outcome, "output": output if outcome == "succeeded" else None},
    )


def _turn(steps: list[list[Item]], *, model_steps: int | None = None) -> Turn:
    return Turn(
        turn_id=uuid4(),
        request_id="ledger",
        request_fingerprint="0" * 64,
        status=TurnStatus.EXECUTING_TOOLS,
        budget=Budget(max_steps=100),
        items=(
            _item(TextContent(kind="user_message", text="fix the bug")),
            *(item for step in steps for item in step),
        ),
        model_steps=len(steps) if model_steps is None else model_steps,
        created_at=utc_now(),
    )


def _thread(*turns: Turn, **fields: object) -> Thread:
    now = utc_now()
    return Thread(
        thread_id=uuid4(), workspace="/w", turns=turns, created_at=now, updated_at=now, **fields
    )


def notice(*steps: list[Item], rollback_available: bool = True) -> str | None:
    """这些步骤依次发生后，宿主对最后一步给出的说明。"""
    turn = _turn(list(steps))
    return CommandLedgerNotices(rollback_available=rollback_available).step_notice(
        _thread(turn), turn
    )


def test_a_check_that_passed_before_a_patch_and_fails_after_it_is_reported() -> None:
    transaction = uuid4()

    reported = notice(run(), patch(transaction), run(returncode=1))

    assert reported == (
        f"`{CHECK}` exited 0 at step 1 and exits 1 now (step 3). Workspace patches applied in "
        f"between: transaction_id={transaction} ({SOURCE}). The latest one can be narrowed with "
        f"another patch or undone with rollback_workspace_patch (transaction_id={transaction})."
    )


def test_a_command_that_never_passed_is_never_reported() -> None:
    assert notice(run(returncode=1), patch(uuid4()), run(returncode=1)) is None
    assert notice(patch(uuid4()), run(returncode=2)) is None
    assert notice(run("other check"), patch(uuid4()), run(returncode=1)) is None


def test_a_failure_that_predates_the_patch_is_not_attributed_to_it() -> None:
    assert notice(run(), run(returncode=1)) is None
    # 第一次失败时还没有补丁；其后的补丁不是从通过变成失败的原因。
    assert notice(run(), run(returncode=1), patch(uuid4()), run(returncode=1, stdout="x")) is None


def test_a_regression_is_reported_once_until_the_command_passes_again() -> None:
    first, second, third, fourth = uuid4(), uuid4(), uuid4(), uuid4()
    broken = [run(), patch(first), run(returncode=1)]
    still_failing = [*broken, patch(second), run(returncode=1, stdout="again")]
    fixed = [*still_failing, patch(third), run()]
    broken_again = [*fixed, patch(fourth), run(returncode=1)]

    assert str(first) in (notice(*broken) or "")
    assert notice(*still_failing) is None
    assert notice(*fixed) is None
    again = notice(*broken_again) or ""
    assert "exited 0 at step 7 and exits 1 now (step 9)" in again
    assert str(fourth) in again
    assert all(str(earlier) not in again for earlier in (first, second, third))


def test_the_report_does_not_depend_on_the_model_history_window() -> None:
    """压缩后模型历史里已没有通过的那次运行；账本读的是Thread原始Item，结论不变。"""
    steps = [run(), patch(uuid4()), run(returncode=1)]
    turn = _turn(steps)
    kept = [item.item_id for item in turn.items[-4:]]
    window = CompactionWindow(
        window_id=uuid4(),
        compaction_id=uuid4(),
        source_finished_event_sequence=20,
        activated_event_sequence=21,
        model_step=3,
        history_item_ids=tuple(kept),
        history_sha256="1" * 64,
        history_tokens=100,
        raw_history_items=len(turn.items),
        raw_history_last_item_id=turn.items[-1].item_id,
        raw_history_ids_sha256="2" * 64,
        activated_at=utc_now() + timedelta(seconds=1),
    )
    compacted = _thread(
        turn, compaction_windows=(window,), active_compaction_window_id=window.window_id
    )
    passing_run = turn.items[3]
    assert isinstance(passing_run.content, ToolResultContent)
    assert passing_run.item_id not in window.history_item_ids

    source = CommandLedgerNotices(rollback_available=True)

    assert source.step_notice(compacted, turn) == source.step_notice(_thread(turn), turn)
    assert "exited 0 at step 1" in (source.step_notice(compacted, turn) or "")


def test_a_killed_or_timed_out_run_is_not_an_exit() -> None:
    transaction = uuid4()
    # 超时被终止的进程同样带着退出码（如-9），但终止原因不是exited。
    timed_out = [run(), patch(transaction), run(returncode=-9, stop_reason="timeout")]

    assert notice(*timed_out) is None
    # 上一次真正退出的运行仍是通过的那次。
    assert "exited 0 at step 1" in (notice(*timed_out, run(returncode=1)) or "")


def test_only_a_successful_patch_counts_as_a_workspace_change() -> None:
    rejected = _step("apply_patch_batch", {"files": []}, {"outcome": "failed", "output": None})
    assert notice(run(), rejected, run(returncode=1)) is None
    uncertain = {**patch_result(uuid4()), "outcome": "unknown"}
    assert notice(run(), _step("apply_patch_batch", {}, uncertain), run(returncode=1)) is None
    assert notice(run(), rollback(uuid4(), outcome="failed"), run(returncode=1)) is None
    # 回滚同样改变了工作区。
    assert "Workspace patches applied in between" in (
        notice(run(), rollback(uuid4()), run(returncode=1)) or ""
    )


def test_commands_match_by_tool_and_normalised_text() -> None:
    between = patch(uuid4())
    assert notice(run("make  test"), between, run("make  test \r\n", returncode=1)) is not None
    # 行内空白可能有语义，不视为同一条命令。
    assert notice(run("make  test"), between, run("make test", returncode=1)) is None
    assert notice(run(), between, run(returncode=1, tool="host_command.sandbox")) is None
    # 固定Profile的检查不是Shell命令，不在账本里。
    profile = "run_profile.unit"
    assert notice(run(tool=profile), between, run(returncode=1, tool=profile)) is None


def test_the_report_is_bounded_for_long_commands_and_many_patches() -> None:
    command = "printf start; " + "echo padding; " * 1000 + "./run-tests the-last-part"
    paths = tuple(f"{'deep/' * 30}module_{index}.py" for index in range(5))
    patches = [patch(uuid4(), diff=str(index), paths=paths) for index in range(6)]

    reported = notice(run(command), *patches, run(command, returncode=1)) or ""

    assert len(reported) < 1200
    assert "printf start;" in reported and "the-last-part" in reported
    assert "3 earlier, then transaction_id=" in reported
    assert reported.count("transaction_id=") == 4
    assert "+3 more" in reported and "…" in reported


def test_the_rollback_hint_needs_the_rollback_tool() -> None:
    steps = [run(), patch(uuid4()), run(returncode=1)]

    assert "rollback_workspace_patch" in (notice(*steps) or "")
    without = notice(*steps, rollback_available=False) or ""
    assert "exited 0 at step 1" in without
    assert "rollback_workspace_patch" not in without


def test_a_pass_in_an_earlier_turn_is_named_as_such() -> None:
    earlier = _turn([run()]).model_copy(update={"status": TurnStatus.COMPLETED})
    current = _turn([patch(uuid4()), run(returncode=1)])

    reported = CommandLedgerNotices().step_notice(_thread(earlier, current), current) or ""

    assert "exited 0 in an earlier turn and exits 1 now (step 2)" in reported


def test_step_numbers_follow_the_runtime_step_counter() -> None:
    """有的模型步骤不留下任何Item；说明里的步骤号以运行时计数为准，从当前步骤往回数。"""
    turn = _turn([run(), patch(uuid4()), run(returncode=1)], model_steps=5)

    reported = CommandLedgerNotices().step_notice(_thread(turn), turn) or ""

    assert "exited 0 at step 3 and exits 1 now (step 5)" in reported


def test_the_same_patch_rolled_back_twice_is_reported_once() -> None:
    assert REPEATED_ROLLBACK_THRESHOLD == 2
    first, second, third = uuid4(), uuid4(), uuid4()
    once = [patch(first, diff="flip"), rollback(first)]
    twice = [*once, patch(second, diff="flip"), rollback(second)]
    thrice = [*twice, patch(third, diff="flip"), rollback(third)]

    assert notice(*once) is None
    assert notice(*twice) == (
        f"The same patch to {SOURCE} has now been applied and rolled back 2 times in this "
        "thread; its diff was identical each time."
    )
    assert notice(*thrice) is None


def test_rolling_back_different_patches_is_not_an_oscillation() -> None:
    first, second = uuid4(), uuid4()

    assert (
        notice(
            patch(first, diff="one"), rollback(first), patch(second, diff="two"), rollback(second)
        )
        is None
    )
    # 账本没有见过的事务（例如其他Thread的补丁）不计入。
    assert notice(rollback(uuid4()), rollback(uuid4())) is None


def test_a_command_repeated_with_identical_results_is_reported_at_the_third_run() -> None:
    assert REPEATED_RUN_THRESHOLD == 3
    status = "git status --short"
    twice = [run(status, stdout=" M a"), run(status, stdout=" M a")]

    assert notice(*twice) is None
    assert notice(*twice, run(status, stdout=" M a")) == (
        f"`{status}` has now run 3 times with exit code 0 and byte-identical output; no "
        "workspace patch was applied between these runs."
    )
    assert notice(*twice, run(status, stdout=" M a"), run(status, stdout=" M a")) is None
    # 其间穿插别的命令不算工作区变化。
    assert notice(*twice, run("ls"), run(status, stdout=" M a")) is not None


def test_a_repeat_with_new_information_is_not_reported() -> None:
    status = "git status --short"
    twice = [run(status, stdout=" M a"), run(status, stdout=" M a")]

    assert notice(*twice, run(status, stdout=" M a\n M b")) is None
    assert notice(*twice, run(status, stdout=" M a", returncode=1)) is None
    assert notice(*twice, patch(uuid4()), run(status, stdout=" M a")) is None
    # 补丁之后重新计数。
    after_patch = [*twice, patch(uuid4()), *(run(status, stdout=" M a") for _ in range(3))]
    assert notice(*after_patch) is not None


def test_a_failing_command_repeated_unchanged_is_reported_too() -> None:
    failing = [run(returncode=1, stdout="1 failed") for _ in range(3)]

    assert "has now run 3 times with exit code 1" in (notice(*failing) or "")


def test_facts_of_one_step_are_reported_together() -> None:
    transaction = uuid4()
    other = "./run-tests integration"
    last = [*run(returncode=1)[:2], *run(other, returncode=1)[1:2]]
    # 一次响应里的两个调用，结果依次落账。
    first_call, second_call = last[1].content, last[2].content
    assert isinstance(first_call, ToolCallContent) and isinstance(second_call, ToolCallContent)
    results = [
        _item(ToolResultContent(call_id=call.call_id, **exit_result(1)))
        for call in (first_call, second_call)
    ]

    reported = notice(run(), run(other), patch(transaction), [*last, *results]) or ""

    assert reported.count("\n") == 1
    assert f"`{CHECK}` exited 0 at step 1" in reported
    assert f"`{other}` exited 0 at step 2" in reported


def test_an_apply_and_rollback_loop_followed_by_idle_verification() -> None:
    """同一份补丁应用又回滚六次、随后反复跑同一条验证命令的会话：两类事实各说明一次。"""
    verify = "git diff --stat && ./run-tests unit | tail -2"
    steps: list[list[Item]] = []
    reported: list[tuple[int, str]] = []
    for _ in range(6):
        transaction = uuid4()
        steps += [patch(transaction, diff="operator flip"), rollback(transaction)]
        if (fact := notice(*steps)) is not None:
            reported.append((len(steps), fact))
    for _ in range(8):
        steps.append(run(verify, stdout="OK (2 tests)"))
        if (fact := notice(*steps)) is not None:
            reported.append((len(steps), fact))

    assert [step for step, _ in reported] == [4, 15]
    assert "applied and rolled back 2 times" in reported[0][1]
    assert "has now run 3 times with exit code 0" in reported[1][1]


def test_a_check_that_hides_its_exit_status_is_out_of_reach() -> None:
    """已知边界：检查把失败只写进输出、退出码仍为0时（`…; echo exit=$?`、`… | tail`），
    宿主没有“通过变失败”的事实可说；输出不同也不算原地重复。"""
    masked = "javac -d classes @sources.txt 2>&1 | grep -v deprecat; java -cp classes Repro"

    assert (
        notice(
            run(masked, stdout="reader {}: OK"),
            patch(uuid4()),
            run(masked, stdout="reader {}: FAIL IndexOutOfBounds"),
        )
        is None
    )


class ScriptedTools:
    """把命令与补丁工具注册为只读替身，按调用顺序返回预先写好的结果。"""

    def __init__(self, *results: dict[str, object]) -> None:
        self.results = list(results)

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            ToolDescriptor(
                name=name,
                version="1",
                description="测试替身",
                input_schema={"type": "object"},
                effect_class=EffectClass.READ_ONLY,
                risk_level=RiskLevel.LOW,
                requires_idempotency=False,
                requires_approval=False,
                supports_reconciliation=False,
                supports_parallel_calls=False,
            )
            for name in (SHELL, "apply_patch_batch")
        )

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        cancel.checkpoint()
        return ToolResultContent(call_id=call.call_id, **self.results.pop(0))


def _call(tool: str, arguments: dict[str, JsonValue]) -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="response"),
        ToolCallCompleted(call_id="call", tool=tool, arguments=arguments),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def _user_messages(request: ModelRequest) -> list[str]:
    return [
        entry.content.text
        for entry in request.history
        if isinstance(entry.content, TextContent) and entry.content.kind == "user_message"
    ]


async def test_runtime_tells_the_model_which_patch_came_before_the_failure(
    tmp_path: Path,
) -> None:
    transaction = uuid4()
    provider = ScriptedProvider(
        [
            _call(SHELL, {"cmd": CHECK}),
            _call("apply_patch_batch", {"files": []}),
            _call(SHELL, {"cmd": CHECK}),
            answer("done"),
        ]
    )
    tools = ScriptedTools(exit_result(), patch_result(transaction), exit_result(1))
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, provider, tools, step_notices=CommandLedgerNotices(rollback_available=True)
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="regress")

    assert completed.status == TurnStatus.COMPLETED
    assert [_user_messages(request) for request in provider.requests[:3]] == [["fix the bug"]] * 3
    notice_text = _user_messages(provider.requests[3])[-1]
    assert notice_text.startswith(
        f"[Harnessix host notice] `{CHECK}` exited 0 at step 1 and exits 1 now (step 3)."
    )
    assert f"transaction_id={transaction} ({SOURCE})" in notice_text
    # 说明紧跟在失败的命令结果之后，是历史的最后一项。
    assert isinstance(provider.requests[3].history[-2].content, ToolResultContent)
    assert provider.requests[3].history[-1].item_id == step_notice_item_id(completed.turn_id, 3)


async def test_runtime_keeps_the_ledger_across_a_restart(tmp_path: Path) -> None:
    """账本不是进程内状态：重新打开Session后，上一轮通过的检查在这一轮失败仍会说明。"""
    store_path = tmp_path / "session.db"
    first = ScriptedProvider([_call(SHELL, {"cmd": CHECK}), answer("passes")])
    async with AgentRuntime(
        SQLiteSessionStore(store_path),
        first,
        ScriptedTools(exit_result()),
        step_notices=CommandLedgerNotices(),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        await runtime.run_turn(thread.thread_id, "check it", request_id="one")

    second = ScriptedProvider(
        [
            _call("apply_patch_batch", {"files": []}),
            _call(SHELL, {"cmd": CHECK}),
            answer("done"),
        ]
    )
    async with AgentRuntime(
        SQLiteSessionStore(store_path),
        second,
        ScriptedTools(patch_result(uuid4()), exit_result(1)),
        step_notices=CommandLedgerNotices(),
    ) as runtime:
        await runtime.run_turn(thread.thread_id, "now change it", request_id="two")

    assert (
        "exited 0 in an earlier turn and exits 1 now (step 2)"
        in (_user_messages(second.requests[2])[-1])
    )
