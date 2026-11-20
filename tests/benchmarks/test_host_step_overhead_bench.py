"""宿主每步开销基准：阶段切分的算术，以及一个完整工具循环在真实产品运行时上跑通。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.context import tool_result_view
from harnessix.context.model_view_memo import ModelViewMemo
from harnessix.protocol.contracts import (
    PublicEvent,
    PublicUsage,
    TurnStatePublicEvent,
    UsagePublicEvent,
)
from scripts.host_step_overhead_bench import (
    PHASES,
    ClockedScriptedProvider,
    format_report,
    run_benchmark,
    step_timings,
)

_START = datetime(2026, 11, 11, tzinfo=UTC)
_THREAD, _TURN = uuid4(), uuid4()


def _event(cursor: int, at_ms: int, status: str | None) -> PublicEvent:
    data = (
        TurnStatePublicEvent(status=status)
        if status is not None
        else UsagePublicEvent(
            step=1, usage=PublicUsage(input_tokens=1, output_tokens=1, total_tokens=2)
        )
    )
    return PublicEvent(
        event_id=uuid4(),
        thread_id=_THREAD,
        turn_id=_TURN,
        cursor=cursor,
        occurred_at=_START + timedelta(milliseconds=at_ms),
        data=data,
    )


def test_step_timings_split_each_step_at_state_changes_and_subtract_scripted_waits() -> None:
    marks = [
        (0, "preparing_context"),
        (30, "calling_model"),
        (130, None),  # 用量事件：模型阶段结束
        (150, "executing_tools"),
        (160, "waiting_approval"),
        (200, "executing_tools"),
        (260, "preparing_context"),
        (300, "calling_model"),
        (320, None),
        (330, "executing_tools"),
        (350, "preparing_context"),
        (360, "calling_model"),
        (370, None),
        (380, "completed"),
    ]
    events = [_event(index, at, status) for index, (at, status) in enumerate(marks, 1)]

    first, second = step_timings(
        events, ["exec_command.testbed", "grep"], [0.08, 0.0], [1.0, 1.25, 1.5]
    )

    assert (first.step, first.tool, second.step, second.tool) == (
        1,
        "exec_command.testbed",
        2,
        "grep",
    )
    measured = [round(getattr(first, phase) * 1000) for phase in PHASES]
    # 模型阶段100ms里有80ms是脚本等待；审批前后的两段执行合并计入工具阶段。
    assert measured == [30, 20, 20, 40, 70]
    assert [round(getattr(second, phase) * 1000) for phase in PHASES] == [40, 20, 10, 0, 20]
    assert round(first.total * 1000) == 180
    assert (first.cpu, second.cpu) == (0.25, 0.25)


@pytest.mark.skipif(os.name != "posix", reason="外部边界命令Profile只在POSIX上可用")
async def test_one_tool_cycle_runs_on_the_product_runtime_and_times_every_step(
    tmp_path: Path,
) -> None:
    report = await run_benchmark(tmp_path, steps=9)

    assert report.valid, format_report(report)
    assert [item.tool for item in report.steps] == [
        "grep",
        "read_file",
        "exec_command.testbed",
        "grep",
        "read_file",
        "exec_command.testbed",
        "apply_patch_batch",
        "exec_command.testbed",
        "grep",
    ]
    for item in report.steps:
        assert item.preparing_context > 0 and item.model_call > 0 and item.tool_and_result > 0
        assert item.cpu > 0
        # 评测的自动审批覆盖命令与补丁；只读工具不经过审批往返。
        assert (item.approval > 0) == (item.tool in {"exec_command.testbed", "apply_patch_batch"})
    assert report.session_bytes > 0
    patched = (tmp_path / "workspace" / "inventory" / "warehouse_07.py").read_text("utf-8")
    assert "# patched-07-07" in patched and "# marker-07-07" not in patched


def _serialized(prepared: tool_result_view.PreparedModelHistory) -> str:
    return json.dumps(
        [
            [item.model_dump_json() for item in prepared.history],
            prepared.inspection.model_dump_json(),
            [decision.model_dump_json() for decision in prepared.new_decisions],
            [
                [str(ref.call_id), ref.binding.model_dump_json(), ref.omitted_field]
                for ref in prepared.references
            ],
        ]
    )


@pytest.mark.skipif(os.name != "posix", reason="外部边界命令Profile只在POSIX上可用")
async def test_reused_model_view_equals_from_scratch_at_every_call_of_a_product_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型请求的历史与模型历史事件都取自这一个函数的返回值：每次调用都用同一份输入再逐项重算
    一遍并逐字节比对，再核对每个出站请求的历史就是某次逐项重算的结果。两次独立运行的ID与时间戳
    不同，无法直接逐字节比对。"""
    reused_memo = ModelViewMemo()
    monkeypatch.setattr(tool_result_view, "_MEMO", reused_memo)
    original = tool_result_view.prepare_model_history_items
    compared: list[int] = []
    different: list[int] = []
    scratch_histories: set[str] = set()
    sent_histories: list[str] = []

    def both(*args: object, **kwargs: object) -> tool_result_view.PreparedModelHistory:
        reused = original(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(tool_result_view, "_MEMO", ModelViewMemo(max_entries=0))
            scratch = original(*args, **kwargs)
        compared.append(len(reused.history))
        if reused != scratch or _serialized(reused) != _serialized(scratch):
            different.append(len(compared))
        scratch_histories.add(json.dumps([item.model_dump_json() for item in scratch.history]))
        return reused

    send = ClockedScriptedProvider.stream

    def recording_stream(self, request, cancel):  # type: ignore[no-untyped-def]
        sent_histories.append(json.dumps([item.model_dump_json() for item in request.history]))
        return send(self, request, cancel)

    monkeypatch.setattr(tool_result_view, "prepare_model_history_items", both)
    monkeypatch.setattr(ClockedScriptedProvider, "stream", recording_stream)

    report = await run_benchmark(tmp_path, steps=9)

    assert report.valid, format_report(report)
    assert different == []
    assert len(sent_histories) == 10 and set(sent_histories) <= scratch_histories
    # 每个模型步骤三次：准备、提交前复核、Reducer核对；共九个工具步骤加一个收尾步骤。
    assert len(compared) >= 30 and max(compared) > 20
    assert len(reused_memo) > 20
