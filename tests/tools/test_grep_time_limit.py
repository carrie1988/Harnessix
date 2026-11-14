"""读取时限到了而已有命中时返回这些命中（ADR 0162，Beta Issue #24）。

Windows上第一次搜索新检出的Dify，读文件内容要先经Defender扫描：PowerShell的Select-String
第一次要13秒，Harnessix的整仓grep在5秒时限处失败、一条结果也没有。现在快照与正则Grep
返回已找到的命中，标为time_limit；没有命中、或原字面量Grep，照旧以超时失败。两个平台的
实现各自验证。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from harnessix.tools import search
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.search import SearchCapture
from harnessix.tools.search_contracts import GrepInput, GrepSnapshotInput, RegexGrepInput
from harnessix.tools.windows_read import WindowsReadRuntime
from harnessix.tools.workspace import ReadOperation, Workspace, pinned_root
from tests.tools.test_pinned_root import POSIX_ONLY
from tests.tools.test_windows_read_adapter import _FakeWindowsRoot


class _ExpiresAfterFirstHit(ReadOperation):
    """第一条命中出现后，下一次检查点即报读取超时（模拟5秒时限到了）。"""

    def __init__(self) -> None:
        super().__init__()
        self.hit = False

    def checkpoint(self) -> None:
        super().checkpoint()
        if self.hit:
            raise ReadToolError("timeout")


@pytest.fixture
def first_hit_expires(monkeypatch: pytest.MonkeyPatch) -> _ExpiresAfterFirstHit:
    operation = _ExpiresAfterFirstHit()
    original = search.grep_line_matches

    def marking(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for hit in original(*args, **kwargs):
            operation.hit = True
            yield hit

    monkeypatch.setattr(search, "grep_line_matches", marking)
    return operation


def _repository(root: Path) -> None:
    for index in range(6):
        (root / f"m{index}").mkdir()
        (root / f"m{index}/a.ts").write_text("export const useTarget = 1;\n", encoding="utf-8")


def _posix(root: Path) -> Callable[[Any, ReadOperation, SearchCapture | None], Any]:
    def run(args: Any, operation: ReadOperation, capture: SearchCapture | None) -> Any:
        with Workspace(root) as workspace, pinned_root(workspace, operation):
            return search.grep(workspace, args, operation, capture=capture)

    return run


def _windows(root: Path) -> Callable[[Any, ReadOperation, SearchCapture | None], Any]:
    runtime = WindowsReadRuntime(root, root_factory=cast(Any, _FakeWindowsRoot))
    return lambda args, operation, capture: runtime.execute(args, operation, capture=capture)


PLATFORMS = [
    pytest.param(_posix, marks=POSIX_ONLY, id="posix"),
    pytest.param(_windows, id="windows"),
]


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("input_type", [GrepSnapshotInput, RegexGrepInput])
def test_matches_found_before_the_time_limit_are_returned(
    tmp_path: Path, first_hit_expires: _ExpiresAfterFirstHit, platform: Any, input_type: Any
) -> None:
    _repository(tmp_path)
    capture = SearchCapture()
    result = platform(tmp_path)(input_type(query="useTarget"), first_hit_expires, capture)
    assert [hit.path for hit in result.matches] == ["m0/a.ts"]
    assert result.truncated and result.truncation_reason == "time_limit"
    assert not result.scan_complete and not capture.complete


@pytest.mark.parametrize("platform", PLATFORMS)
def test_without_matches_or_on_the_original_grep_the_timeout_stays_a_failure(
    tmp_path: Path, first_hit_expires: _ExpiresAfterFirstHit, platform: Any
) -> None:
    _repository(tmp_path)
    expired = ReadOperation(timeout_seconds=0.001)
    expired.deadline = 0  # 已经超时：一条命中也没有
    with pytest.raises(ReadToolError, match="timeout"):
        platform(tmp_path)(GrepSnapshotInput(query="useTarget"), expired, None)
    with pytest.raises(ReadToolError, match="timeout"):
        platform(tmp_path)(GrepInput(query="useTarget"), first_hit_expires, None)


class _ExpiresAfter(ReadOperation):
    """第N次检查点之后报读取超时：模拟时限落在遍历后面的目录时。"""

    def __init__(self, checkpoints: int) -> None:
        super().__init__()
        self.remaining = checkpoints

    def checkpoint(self) -> None:
        super().checkpoint()
        self.remaining -= 1
        if self.remaining < 0:
            raise ReadToolError("timeout")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_matches_are_read_while_the_walk_goes_on(tmp_path: Path, platform: Any) -> None:
    """ADR 0162第2版：边遍历边读。时限落在遍历后面的目录时，前面目录里的命中照样返回；
    此前要先遍历完全部目录才开始读，时限落在遍历阶段时一条命中也没有（Windows真机上的Dify）。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "a/hit.ts").write_text("useTarget\n", encoding="utf-8")
    for letter in "bcdefghijklmnopqrstuvwxyz":
        (tmp_path / letter).mkdir()
        for index in range(40):
            (tmp_path / letter / f"f{index}.ts").write_text("other\n", encoding="utf-8")
    result = platform(tmp_path)(GrepSnapshotInput(query="useTarget"), _ExpiresAfter(300), None)
    assert [hit.path for hit in result.matches] == ["a/hit.ts"]
    assert result.truncation_reason == "time_limit"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_the_walk_yields_files_in_the_same_order_as_sorting_full_paths(
    tmp_path: Path, platform: Any
) -> None:
    """按名称（目录为“名称/”）排序的深度优先顺序，与收集完再按完整路径排序相同。"""
    paths = ["a.ts", "a-b/x.ts", "a/x.ts", "a/b/c.ts", "a0.ts", "a b/x.ts", "b.ts", "a/b.ts"]
    for path in paths:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("useTarget\n", encoding="utf-8")
    args = GrepSnapshotInput(query="useTarget", max_results=50)
    result = platform(tmp_path)(args, ReadOperation(), None)
    assert [hit.path for hit in result.matches] == sorted(paths)
    limited = platform(tmp_path)(
        GrepSnapshotInput(query="useTarget", max_results=3), ReadOperation(), None
    )
    assert [hit.path for hit in limited.matches] == sorted(paths)[:3]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reading_while_walking_keeps_the_include_rules(tmp_path: Path, platform: Any) -> None:
    """边遍历边读不改变include的规则（ADR 0129）：起点是文件时不筛选；整棵树走完、有文件却无一
    符合include时仍报no_files_searched。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("useTarget\n", encoding="utf-8")
    run = platform(tmp_path)
    with pytest.raises(ReadToolError, match="no_files_searched"):
        run(GrepSnapshotInput(query="useTarget", include="*.ts"), ReadOperation(), None)
    single = run(
        GrepSnapshotInput(query="useTarget", path="src/a.py", include="*.ts"), ReadOperation(), None
    )
    assert [hit.path for hit in single.matches] == ["src/a.py"]
