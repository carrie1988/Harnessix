"""长历史宿主开销基准：脚本化Turn在真实产品运行时上跑通，阶段切分覆盖每个工具步骤。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.long_history_overhead_bench import (
    PHASES,
    build_long_script,
    format_report,
    run_long_benchmark,
    specification,
    tool_call,
)


def test_one_cycle_creates_reads_edits_lists_and_runs_the_same_module() -> None:
    kinds = ("create", "show", "read", "edit", "list", "test_file", "run_test", "spec")
    calls = [tool_call(kind, step) for step, kind in enumerate(kinds, 9)]
    assert [tool for tool, _ in calls] == [
        "apply_patch_batch",
        "exec_command.testbed",
        "read_file",
        "apply_patch_batch",
        "exec_command.testbed",
        "apply_patch_batch",
        "exec_command.testbed",
        "read_file",
    ]
    # 第9–16步围绕同一个模块编号1。
    assert calls[0][1]["files"][0]["path"] == "pkg/mod_001.py"
    assert calls[6][1]["cmd"] == "python3 tests/test_mod_001.py"
    script, tools = build_long_script(16)
    assert len(script) == 17 and len(tools) == 16
    assert len(specification().encode()) > 30_000


@pytest.mark.skipif(os.name != "posix", reason="外部边界命令Profile只在POSIX上可用")
async def test_a_short_long_history_turn_runs_on_the_product_runtime(tmp_path: Path) -> None:
    report = await run_long_benchmark(tmp_path, steps=9)

    assert report.valid, format_report(report, 9)
    assert len(report.steps) == 9 and report.summaries == 0
    for item in report.steps:
        assert item.preparing_context > 0 and item.pre > 0 and item.post > 0
        assert item.estimated_input_bytes > len(specification().encode())
        # 评测的自动审批覆盖命令与补丁；只读工具不经过审批往返。
        assert (item.waiting_approval > 0) == (item.tool != "read_file")
    sizes = [item.estimated_input_bytes for item in report.steps]
    assert sizes == sorted(sizes)
    (row,) = report.buckets(9)
    assert set(PHASES) <= set(row)
    test_output = (tmp_path / "workspace" / "tests" / "test_mod_000.py").read_text("utf-8")
    assert "test_case_15 PASSED" in test_output
