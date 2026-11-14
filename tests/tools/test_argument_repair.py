"""读工具参数修复：评测中被拒的真实写法经修复后通过原有严格校验；不可修复的照常拒绝。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.artifacts.contracts import ReadArtifactInput
from harnessix.tools import runtime as tool_runtime
from harnessix.tools import search, windows_search
from harnessix.tools.argument_repair import repair_read_arguments
from harnessix.tools.contracts import ListFilesInput, ReadFileInputV4
from harnessix.tools.read_backend import _ReadBackend
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GlobInput, GrepSnapshotInput, RegexGrepInput
from harnessix.tools.windows_read import WindowsReadRuntime
from tests.tools.test_files import call, execute
from tests.tools.test_windows_read_adapter import _FakeWindowsRoot

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
        # 位置也接受纯数字字符串，但不截断。
        ({"path": "a.py", "start_line": "38"}, {"path": "a.py", "start_line": 38}),
        # grep的context_lines用在read_file上即定位点之前的行数（fluentd-3466、hugo-12204、
        # terraform-35611 GQWePot）：0不改变读取，其余按lines_before处理。
        (
            {"path": "a.py", "context_lines": "0", "start_line": 1},
            {"path": "a.py", "lines_before": 0, "start_line": 1},
        ),
        (
            {"path": "a.py", "context_lines": "2", "max_lines": 120, "start_line": 4911},
            {"path": "a.py", "max_lines": 120, "start_line": 4909},
        ),
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
        (ListFilesInput, {"path": ".", "offset": "0"}, {"path": ".", "offset": 0}),
    ],
)
def test_search_and_listing_repairs(model, arguments, repaired):
    tool = {GlobInput: "glob", ListFilesInput: "list_files"}.get(model, "grep")
    assert repair_read_arguments(tool, arguments, model) == repaired
    model.model_validate(repaired)


def test_read_artifact_paging_accepts_numeric_text_and_caps_the_page():
    # 回归（lucene-13494、javaparser-4538、preact-3567等5次）：存档分页参数是数字字符串。
    arguments = {"artifact_id": ARTIFACT_ID, "offset": "0", "limit": "500"}
    repaired = repair_read_arguments("read_artifact", arguments, ReadArtifactInput)
    assert repaired == {"artifact_id": ARTIFACT_ID, "offset": 0, "limit": 200}
    ReadArtifactInput.model_validate_json(json.dumps(repaired))
    # 非数字文本与负数照常交严格校验拒绝。
    for invalid in ({"limit": "many"}, {"offset": -1}):
        unchanged = {"artifact_id": ARTIFACT_ID, **invalid}
        assert repair_read_arguments("read_artifact", unchanged, ReadArtifactInput) == unchanged


@pytest.mark.parametrize(
    ("arguments", "repaired"),
    [
        # 回归（babel-14532 BGuCkgK、nushell-13831 kAnJL9M、laravel-53949 q3NqyRo）：从根遍历
        # 整个仓库才筛到一个目录，条目数先触发了扫描上限。
        (
            {"pattern": "packages/generator/test/fixtures/parentheses/*"},
            {"path": "packages/generator/test/fixtures/parentheses", "pattern": "*"},
        ),
        ({"pattern": "tests/**/*Once*"}, {"path": "tests", "pattern": "**/*Once*"}),
        ({"pattern": "src/app.py", "path": "."}, {"path": "src", "pattern": "app.py"}),
    ],
)
def test_glob_starts_at_the_literal_directory_of_the_pattern(arguments, repaired):
    assert repair_read_arguments("glob", arguments, GlobInput) == repaired
    GlobInput.model_validate(repaired)


@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "**/split_column*"},
        {"pattern": "*.py"},
        # path另有取值时pattern相对它解释。
        {"pattern": "src/*.py", "path": "lib"},
        # 从根遍历会跳过默认忽略的目录，移到path则会进入：结果不同，不换算。
        {"pattern": "node_modules/pkg/*.js"},
        # 不合法的pattern不借换算绕过严格校验。
        {"pattern": "x/" * 33 + "y"},
        {"pattern": "../a/*.py"},
        {"pattern": "/tmp/a/*.py"},
    ],
)
def test_glob_patterns_without_a_safe_literal_directory_are_untouched(arguments):
    assert repair_read_arguments("glob", arguments, GlobInput) == arguments


def use_windows_backend(monkeypatch, root):
    """在任何平台上走Windows搜索实现（伪造的原生根），验证两个平台的glob行为相同。"""
    backend = _ReadBackend(
        workspace=None,
        windows=WindowsReadRuntime(root, root_factory=cast(Any, _FakeWindowsRoot)),
        implementation="coding-read/windows-v1",
        git_runtime=None,
    )
    monkeypatch.setattr(tool_runtime, "build_read_backend", lambda *_arguments: backend)


@pytest.mark.parametrize("backend", ["native", "windows"])
async def test_glob_under_a_literal_directory_is_not_stopped_by_the_rest_of_the_tree(
    tmp_path, monkeypatch, backend
):
    if backend == "windows":
        use_windows_backend(monkeypatch, tmp_path)
    (tmp_path / "crates" / "split").mkdir(parents=True)
    (tmp_path / "crates" / "split" / "column.rs").write_text("x\n")
    (tmp_path / "crates" / "split" / "row.rs").write_text("x\n")
    for index in range(8):
        (tmp_path / f"other{index}.txt").write_text("x\n")
    async with CodingToolRuntime(tmp_path) as tools:
        whole = await execute(tools, "glob", pattern="crates/split/*", path=".")
        expected = ["crates/split/column.rs", "crates/split/row.rs"]
        assert whole.output["paths"] == expected
        # 两个平台的搜索实现各自导入上限，都要调低；只调POSIX时Windows的整仓glob不会超限。
        monkeypatch.setattr(search, "MAX_SEARCH_ENTRIES", 4)
        monkeypatch.setattr(windows_search, "MAX_SEARCH_ENTRIES", 4)
        narrowed = await execute(tools, "glob", pattern="crates/split/*")
        assert narrowed.outcome == "succeeded" and narrowed.output["paths"] == expected
        unbounded = await execute(tools, "glob", pattern="**/column.rs")
        assert unbounded.error.code == "tool_limit_exceeded"
        assert unbounded.error.message.startswith("The scan hit a tool limit")
        # 字面目录不存在：原来是空结果，现在如实报告路径不存在。
        absent = await execute(tools, "glob", pattern="crates/missing/*")
        assert absent.error.code == "tool_not_found"


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
    # 按字节写：write_text在Windows上会把\n写成\r\n。
    (tmp_path / "app.py").write_bytes(b"one\ntwo\nthree\nfour\n")
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
