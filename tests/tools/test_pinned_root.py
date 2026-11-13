"""一次读取操作只在开头与结尾核对根（Beta Issue #21）。

0.10.0b32/b33：在16000个文件的仓库里整仓grep，5秒读取时限内搜不完，没有任何结果。性能分析：每打开一个文件，
前后都要从“/”逐段重新打开并核对根（Windows从盘符开始），这部分占了一半以上的时间。现在固定的根只属于
这一次操作；其间根路径被替换，读取不会落到别处，结束时报workspace_changed。
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest

from harnessix.tools import search
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.search_contracts import GrepSnapshotInput
from harnessix.tools.workspace import ReadOperation, Workspace, pinned_root
from harnessix.workspace.windows import _ancestors

# Workspace是POSIX的no-follow读根；Windows的读取见test_windows_directory_cache.py。
POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="Workspace只支持POSIX")


def _repository(root: Path, files: int) -> None:
    for index in range(files):
        directory = root / "web" / f"part{index % 7}" / "hooks"
        directory.mkdir(parents=True, exist_ok=True)
        body = "export const value = 1;\n" + (
            "export function useTarget() {}\n" if index == 5 else ""
        )
        (directory / f"file{index}.ts").write_text(body, encoding="utf-8")


def _count_root_walks(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    walks: list[int] = []
    original = workspace._open_root

    def counted() -> int:
        walks.append(1)
        return original()

    monkeypatch.setattr(workspace, "_open_root", counted)
    return walks


@POSIX_ONLY
def test_a_whole_repository_search_checks_the_root_twice_not_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repository(tmp_path, 60)
    args = GrepSnapshotInput(query="useTarget", path=".", include="*.ts", max_results=5)
    with Workspace(tmp_path) as workspace:
        walks = _count_root_walks(workspace, monkeypatch)
        operation = ReadOperation()
        with pinned_root(workspace, operation):
            result = search.grep(workspace, args, operation)
        assert [hit.path for hit in result.matches] == ["web/part5/hooks/file5.ts"]
        # 开头与结尾各一次；此前每个文件、每个目录前后各一次（这里约140次）。
        assert len(walks) == 2
        assert operation.pinned_root is None

        unpinned = ReadOperation()
        walks.clear()
        assert search.grep(workspace, args, unpinned).matches == result.matches
        assert len(walks) > 100


@POSIX_ONLY
def test_a_root_replaced_during_the_operation_is_never_read_and_is_reported(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("原来的根", encoding="utf-8")
    with Workspace(root) as workspace:
        operation = ReadOperation()
        with pytest.raises(ReadToolError, match="workspace_changed"):
            with pinned_root(workspace, operation):
                root.rename(tmp_path / "old")
                root.mkdir()
                (root / "a.txt").write_text("替换后的根", encoding="utf-8")
                with workspace.open("a.txt", operation, directory=False) as fd:
                    # 固定的描述符指向原来的根：读到的是原内容，不是替换后的目录。
                    assert os.read(fd, 100).decode("utf-8") == "原来的根"
        assert operation.pinned_root is None


@POSIX_ONLY
def test_the_pin_belongs_to_one_operation(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with Workspace(tmp_path) as workspace:
        first, second = ReadOperation(), ReadOperation()
        with pinned_root(workspace, first):
            assert first.pinned_root is not None and second.pinned_root is None
            with pinned_root(workspace, first):  # 嵌套：不重复固定
                pass
            assert first.pinned_root is not None
            with workspace.open("a.txt", second, directory=False):
                pass
        assert first.pinned_root is None


def test_windows_chain_ancestors_cover_every_level() -> None:
    path = PureWindowsPath("C:/Users/dev/src/dify")
    expected = [
        PureWindowsPath("C:/"),
        PureWindowsPath("C:/Users"),
        PureWindowsPath("C:/Users/dev"),
        PureWindowsPath("C:/Users/dev/src"),
        PureWindowsPath("C:/Users/dev/src/dify"),
    ]
    assert _ancestors(path) == expected  # type: ignore[arg-type]
