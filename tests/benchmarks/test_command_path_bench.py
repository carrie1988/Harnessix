"""命令路径基准：脚本形状、分位数算术，以及三条命令在真实产品运行时上逐阶段计时。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.models.contracts import ToolCallCompleted
from scripts.command_path_bench import (
    COMMAND_TOOL,
    FOLLOWING_PHASES,
    ROUND_TRIP_PHASES,
    build_command_script,
    format_report,
    quartiles,
    run_command_bench,
    summary,
    trivial_command,
)


def test_script_is_one_patch_then_trivial_commands_then_a_text_only_step() -> None:
    script, tools = build_command_script(6)

    assert tools == ["apply_patch_batch", *[COMMAND_TOOL] * 6]
    calls = [[event for event in step if isinstance(event, ToolCallCompleted)] for step in script]
    assert [len(step) for step in calls] == [1, 1, 1, 1, 1, 1, 1, 0]
    issued = [step[0].arguments["cmd"] for step in calls[1:7]]
    assert issued == [trivial_command(step) for step in range(2, 8)]
    # 五类只读命令轮流出现，没有管道、重定向或写入。
    assert {command.split()[0] for command in issued} == {"ls", "sed", "grep", "head", "wc"}
    assert not any(symbol in command for command in issued for symbol in "|>;&")


def test_quartiles_are_inclusive_and_degenerate_to_the_single_sample() -> None:
    assert quartiles([0.4]) == (0.4, 0.4, 0.4)
    assert quartiles([0.1, 0.2, 0.3, 0.4, 0.5]) == pytest.approx((0.2, 0.3, 0.4))


@pytest.mark.skipif(os.name != "posix", reason="外部边界命令Profile只在POSIX上可用")
async def test_three_commands_run_on_the_product_runtime_and_every_phase_is_timed(
    tmp_path: Path,
) -> None:
    report = await run_command_bench(tmp_path, commands=3)

    assert report.valid, format_report(report)
    assert [item.command for item in report.commands] == [trivial_command(n) for n in (2, 3, 4)]
    assert [item.tool for item in report.steps] == [COMMAND_TOOL] * 3
    for item in report.commands:
        phases = [getattr(item, name) for name in (*ROUND_TRIP_PHASES, *FOLLOWING_PHASES)]
        # 三处时间戳来自同一台机器的墙钟，按发生顺序相减不应为负。
        assert all(value >= 0 for value in phases), item
        assert item.round_trip == pytest.approx(sum(phases[: len(ROUND_TRIP_PHASES)]))
        assert 0 < item.bare_command < item.round_trip
        assert item.host_overhead == pytest.approx(item.round_trip - item.bare_command)
    rows = summary(report)
    assert set(rows) == {
        *ROUND_TRIP_PHASES,
        *FOLLOWING_PHASES,
        "bare_command",
        "round_trip",
        "host_overhead",
        "step_total",
        "step_cpu",
    }
    assert all(low <= median <= high for low, median, high in rows.values())
    assert "host_overhead" in format_report(report)
