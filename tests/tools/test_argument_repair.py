"""读工具参数修复：评测中被拒的真实写法经修复后通过原有严格校验；不可修复的照常拒绝。"""

from __future__ import annotations

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.tools.argument_repair import repair_read_arguments
from harnessix.tools.contracts import ListFilesInput, ReadFileInputV4
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GlobInput, GrepSnapshotInput, RegexGrepInput
from tests.tools.test_files import call, execute

ARTIFACT_ID = "01a1b916-a6b7-7ca0-97c6-4ccbac662005"


@pytest.mark.parametrize(
    ("arguments", "repaired"),
    [
        # E0c重跑django-10880的原样参数：offset/limit为字符串，另带重复的path_。
        (
            {"path": "a.py", "limit": "100", "offset": "1", "path_": "a.py", "start_line": 1},
            {"path": "a.py", "start_line": 1, "max_lines": 100},
        ),
        (
            {"path": "a.py", "offset": "0", "limit": "60"},
            {"path": "a.py", "start_line": 1, "max_lines": 60},
        ),
        (
            {"file_path": "a.py", "offset": 9, "limit": 5},
            {"path": "a.py", "start_line": 10, "max_lines": 5},
        ),
        (
            {"path": "a.py", "start_line": 5, "end_line": 20},
            {"path": "a.py", "start_line": 5, "max_lines": 16},
        ),
        ({"path": "a.py", "start_line": 10, "lines_before": 3}, {"path": "a.py", "start_line": 7}),
        ({"path": "a.py", "start_line": 2, "lines_before": 9}, {"path": "a.py", "start_line": 1}),
        (
            {"path": "a.py", "anchor": "def f():\n    return 1", "lines_before": 2},
            {"path": "a.py", "anchor": "def f():", "lines_before": 2},
        ),
        ({"path": "a.py", "anchor": "x", "start_line": 5}, {"path": "a.py", "anchor": "x"}),
        (
            {"path": "a.py", "anchor": "x", "lines_before": 2, "lines_after": 10},
            {"path": "a.py", "anchor": "x", "lines_before": 2, "max_lines": 13},
        ),
        (
            {"path": "a.py", "anchor": "x", "lines_before": 55},
            {"path": "a.py", "anchor": "x", "lines_before": 40},
        ),
        ({"path": "a.py", "max_lines": "30"}, {"path": "a.py", "max_lines": 30}),
        ({"path": "a.py", "max_lines": 5000}, {"path": "a.py", "max_lines": 2000}),
    ],
)
def test_read_file_repairs_common_model_conventions(arguments, repaired):
    original = dict(arguments)
    assert repair_read_arguments("read_file", arguments, ReadFileInputV4) == repaired
    ReadFileInputV4.model_validate(repaired)
    assert arguments == original  # 只返回副本，审计保留模型原始参数


@pytest.mark.parametrize(
    "arguments",
    [
        # 存档分页参数：保留原样，由原有提示改用read_artifact。
        {"artifact_id": ARTIFACT_ID, "offset": "0", "limit": "100"},
        {"path": ARTIFACT_ID, "offset": 1, "limit": 2},
        # 把grep或补丁参数发给read_file：不猜测，照常拒绝。
        {"path": "a.py", "query": "x", "context_lines": 3},
        {"path": "a.py", "edits": [{"new_text": "x"}]},
        # 低于下限、浮点与非数字字符串不修复。
        {"path": "a.py", "max_lines": 0},
        {"path": "a.py", "max_lines": 1.0},
        {"path": "a.py", "max_lines": "many"},
    ],
)
def test_read_file_leaves_misrouted_and_invalid_values_for_strict_rejection(arguments):
    repaired = repair_read_arguments("read_file", arguments, ReadFileInputV4)
    with pytest.raises(ValueError):
        ReadFileInputV4.model_validate(repaired)


@pytest.mark.parametrize(
    ("model", "arguments", "repaired"),
    [
        (
            GrepSnapshotInput,
            {"query": "x", "context_lines": 30},
            {"query": "x", "context_lines": 20},
        ),
        (
            GrepSnapshotInput,
            {"query": "x", "context_lines": "2"},
            {"query": "x", "context_lines": 2},
        ),
        (
            GrepSnapshotInput,
            {"query": "x", "lines_before": 2, "lines_after": 1},
            {"query": "x", "context_lines": 2},
        ),
        (
            RegexGrepInput,
            {"regex": "a+b", "path": "src"},
            {"query": "a+b", "path": "src", "match_mode": "regex"},
        ),
        # 没有match_mode字段的版本只换名，不添加该字段。
        (GrepSnapshotInput, {"regex": "a+b"}, {"query": "a+b"}),
        (GrepSnapshotInput, {"query": "x", "max_results": 500}, {"query": "x", "max_results": 200}),
        (GlobInput, {"pattern": "*.py", "limit": "50"}, {"pattern": "*.py", "max_results": 50}),
        (ListFilesInput, {"path": ".", "limit": 300}, {"path": ".", "limit": 200}),
    ],
)
def test_search_and_listing_repairs(model, arguments, repaired):
    tool = {GlobInput: "glob", ListFilesInput: "list_files"}.get(model, "grep")
    assert repair_read_arguments(tool, arguments, model) == repaired
    model.model_validate(repaired)


def test_negative_context_and_unknown_tools_are_untouched():
    assert repair_read_arguments(
        "grep", {"query": "x", "context_lines": -1}, GrepSnapshotInput
    ) == {
        "query": "x",
        "context_lines": -1,
    }
    unchanged = {"cmd": "ls", "limit": "5"}
    assert repair_read_arguments("exec_command.testbed", unchanged, GlobInput) is unchanged


async def test_runtime_executes_repaired_reads_and_keeps_raw_call_arguments(tmp_path):
    (tmp_path / "app.py").write_text("one\ntwo\nthree\nfour\n")
    async with CodingToolRuntime(tmp_path) as tools:
        request = call(tools, path="app.py", offset="1", limit="2")
        result = await tools.execute(request, CancelToken())
        assert result.outcome == "succeeded"
        assert result.output["text"] == "two\nthree\n"
        assert request.arguments == {"path": "app.py", "offset": "1", "limit": "2"}
        anchored = await execute(tools, path="app.py", anchor="three\nfour", start_line=3)
        assert anchored.outcome == "succeeded" and anchored.output["text"].startswith("three\n")
        searched = await execute(tools, "grep", path=".", query="two", context_lines=9)
        assert searched.outcome == "succeeded"


async def test_runtime_still_points_artifact_reads_to_read_artifact(tmp_path):
    async with CodingToolRuntime(tmp_path) as tools:
        paged = await execute(tools, path=ARTIFACT_ID, offset=1, limit=2)
    assert paged.error.code == "tool_invalid_arguments"
    assert "read_artifact(artifact_id, offset, limit)" in paged.error.message
