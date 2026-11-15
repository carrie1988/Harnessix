"""整仓库Glob：与快照Grep同一组上限，不遍历模式不可能命中的子树（ADR 0165，Beta Issue #35）。

0.10.0b46 Windows：在Prettier仓库根用`tests/format/**/class*/**/*.ts`，遍历触发10,000个条目的
扫描上限，一条结果也没有；把`tests/format/typescript`放进path则只扫1,675项、0.2秒返回40条。只跳过无关目录不够：
Prettier的tests/format下就有约1.1万个条目。POSIX与Windows的遍历与上限相同，三个平台同一规则。
原Glob（未启用快照或正则搜索）的遍历、上限与输出合同不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from harnessix.tools import search
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.patterns import PathPattern
from harnessix.tools.search_contracts import (
    GlobInput,
    GlobOutput,
    WholeRepositoryGlobInput,
    WholeRepositoryGlobOutput,
)
from harnessix.tools.windows_read import WindowsReadRuntime
from harnessix.tools.workspace import ReadOperation, Workspace, pinned_root
from tests.tools.test_pinned_root import POSIX_ONLY
from tests.tools.test_windows_read_adapter import _FakeWindowsRoot


@pytest.mark.parametrize(
    ("pattern", "directory", "expected"),
    [
        ("tests/format/**/class*/**/*.ts", ".", True),
        ("tests/format/**/class*/**/*.ts", "tests", True),
        ("tests/format/**/class*/**/*.ts", "tests/format/typescript/x", True),
        ("tests/format/**/class*/**/*.ts", "src", False),
        ("tests/format/**/class*/**/*.ts", "tests/unit", False),
        ("tests/format/**/class*/**/*.ts", "Tests", False),  # 与matches一样区分大小写
        ("src/*.ts", "src", True),
        ("src/*.ts", "src/deep", False),  # 剩下的一段只能是文件名
        ("**/*.ts", "anything/deeper", True),
        ("docs/**", "docs/a/b", True),
        ("*.ts", "src", False),
    ],
)
def test_may_contain_follows_the_same_rules_as_matches(
    pattern: str, directory: str, expected: bool
) -> None:
    assert PathPattern(pattern).may_contain(directory) is expected


def _repository(root: Path) -> None:
    for index in range(30):
        (root / "src" / f"m{index}").mkdir(parents=True)
        (root / "src" / f"m{index}" / "a.ts").write_text("x", encoding="utf-8")
    for name in ("class-a/x.ts", "class-b/deep/y.ts", "other/z.ts"):
        (root / "tests/format/typescript" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "tests/format/typescript" / name).write_text("x", encoding="utf-8")


def _posix(root: Path, args: GlobInput) -> Any:
    operation = ReadOperation()
    with Workspace(root) as workspace, pinned_root(workspace, operation):
        return search.glob(workspace, args, operation)


def _windows(root: Path, args: GlobInput) -> Any:
    runtime = WindowsReadRuntime(root, root_factory=cast(Any, _FakeWindowsRoot))
    return runtime.glob(args, ReadOperation(), capture=None)


PLATFORMS = [
    pytest.param(_posix, marks=POSIX_ONLY, id="posix"),
    pytest.param(_windows, id="windows"),
]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_pattern_with_fixed_directories_only_walks_that_subtree(
    tmp_path: Path, platform: Any
) -> None:
    _repository(tmp_path)
    found = platform(tmp_path, WholeRepositoryGlobInput(pattern="tests/format/**/class*/**/*.ts"))
    assert type(found) is WholeRepositoryGlobOutput
    assert found.paths == (
        "tests/format/typescript/class-a/x.ts",
        "tests/format/typescript/class-b/deep/y.ts",
    )
    # 根目录2项 + tests、format、typescript各1项 + 三个子目录的内容；src下的30个目录不再进入。
    assert found.stats.entries_scanned < 20
    everything = platform(tmp_path, WholeRepositoryGlobInput(pattern="**/*.ts", max_results=100))
    assert len(everything.paths) == 33 and everything.stats.entries_scanned > 60


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_original_glob_contract_is_unchanged(tmp_path: Path, platform: Any) -> None:
    _repository(tmp_path)
    found = platform(tmp_path, GlobInput(pattern="tests/format/**/class*/**/*.ts"))
    assert type(found) is GlobOutput
    assert len(found.paths) == 2 and found.stats.entries_scanned > 60


@pytest.mark.parametrize("platform", PLATFORMS)
def test_whole_repository_glob_uses_the_whole_repository_entry_limit(
    tmp_path: Path, platform: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有固定前缀也可能超过原上限（Prettier的tests/format约1.1万项）；整仓库Glob用快照Grep的上限。"""
    _repository(tmp_path)
    for module in ("search", "windows_search"):
        monkeypatch.setattr(f"harnessix.tools.{module}.MAX_SEARCH_ENTRIES", 20)
    with pytest.raises(ReadToolError, match="limit_exceeded"):
        platform(tmp_path, GlobInput(pattern="**/*.ts", max_results=100))
    found = platform(tmp_path, WholeRepositoryGlobInput(pattern="**/*.ts", max_results=100))
    assert len(found.paths) == 33 and found.scan_complete


async def test_a_glob_over_the_limit_is_not_told_to_glob_first(tmp_path, monkeypatch):
    """上限失败的下一步：grep仍建议先用glob找候选；glob自己超限时改为缩小path（Beta Issue #35）。"""
    from harnessix.tools.read_error_messages import GLOB_LIMIT_HINT, READ_ERROR_MESSAGES
    from harnessix.tools.runtime import CodingToolRuntime
    from tests.tools.test_files import execute

    _repository(tmp_path)
    monkeypatch.setattr("harnessix.tools.search.MAX_SEARCH_ENTRIES", 20)
    monkeypatch.setattr("harnessix.tools.windows_search.MAX_SEARCH_ENTRIES", 20)
    async with CodingToolRuntime(tmp_path) as tools:
        globbed = await execute(tools, "glob", pattern="**/*.ts")
        grepped = await execute(tools, "grep", query="x")
    assert globbed.error.code == grepped.error.code == "tool_limit_exceeded"
    assert globbed.error.message == GLOB_LIMIT_HINT and "glob" not in GLOB_LIMIT_HINT
    assert grepped.error.message == READ_ERROR_MESSAGES["limit_exceeded"]
