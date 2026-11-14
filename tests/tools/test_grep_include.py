"""Grep的include不再让“一个文件也没搜”读成“搜过了没有”：POSIX与原生读取端同一语义。"""

from __future__ import annotations

import pytest

from harnessix.tools.contracts import ReadToolError
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.tools.search_contracts import GrepInput, GrepSnapshotInput, RegexGrepInput
from tests.tools.test_files import execute
from tests.tools.test_regex_grep import run_grep as run_grep  # 同一组POSIX/原生读取端夹具

FILES = {
    "internal/terraform/node_resource.go": b"type RemovedConfig struct{}\n",
    "internal/terraform/eval.go": b"func eval() {}\n",
    "docs/readme.md": b"RemovedConfig is documented here\n",
}


@pytest.mark.parametrize(
    "include",
    [
        # 回归（terraform-35611 GRMvyKf step 4）：path已是单个文件，include却按仓库根写。
        "internal/terraform/*.go",
        # 回归（projectlombok__lombok-3350__w3Z4mqF）：include重复了文件自己的完整路径。
        "internal/terraform/node_resource.go",
        "*.md",
    ],
)
def test_a_single_file_start_is_searched_whatever_the_include(run_grep, include):
    args = RegexGrepInput(
        query="Removed|removed", path="internal/terraform/node_resource.go", include=include
    )

    output = run_grep(args, FILES)

    assert [(hit.path, hit.line) for hit in output.matches] == [
        ("internal/terraform/node_resource.go", 1)
    ]
    assert output.stats.files_read == 1 and output.scan_complete


@pytest.mark.parametrize(
    ("model", "include"),
    [
        # 回归（babel-15649 AyJ2RqG、terraform-35611 GRMvyKf等14个试次26次）：
        # include指向不存在的文件。
        (RegexGrepInput, "internal/terraform/node_module_expander.go"),
        (GrepSnapshotInput, "*.rs"),
        (GrepInput, "internal/*.go"),
    ],
)
def test_an_include_that_matches_no_file_fails_instead_of_looking_empty(run_grep, model, include):
    with pytest.raises(ReadToolError) as error:
        run_grep(model(query="RemovedConfig", include=include), FILES)

    assert error.value.code == "no_files_searched"


def test_matching_includes_still_return_plain_results(run_grep):
    by_name = run_grep(RegexGrepInput(query="RemovedConfig", include="*.go"), FILES)
    assert [hit.path for hit in by_name.matches] == ["internal/terraform/node_resource.go"]
    by_path = run_grep(RegexGrepInput(query="RemovedConfig", include="docs/*.md"), FILES)
    assert [hit.path for hit in by_path.matches] == ["docs/readme.md"]
    # 文件匹配include但内容无命中：这才是“搜过了没有”。
    searched = run_grep(RegexGrepInput(query="absent", include="*.go"), FILES)
    assert searched.matches == () and searched.stats.files_read == 2 and searched.scan_complete


async def test_runtime_reports_the_unsearched_scan_in_english(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n")
    async with CodingToolRuntime(tmp_path, enable_regex_search=True) as tools:
        unsearched = await execute(tools, "grep", query="needle", include="lib/app.py")
        assert unsearched.outcome == "failed"
        assert unsearched.error.code == "tool_no_files_searched"
        assert unsearched.error.message.startswith("No files were searched")
        assert "pass it as path" in unsearched.error.message
        assert unsearched.error.message.isascii() and "lib/app.py" not in unsearched.error.message
        # 空目录没有可筛选的文件：不是include的问题，照常返回空结果。
        (tmp_path / "empty").mkdir()
        empty = await execute(tools, "grep", query="needle", path="empty", include="*.py")
        assert empty.outcome == "succeeded" and empty.output["matches"] == []
