from __future__ import annotations

import hashlib
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import ListFilesInput, ReadFileInput, ReadToolError
from harnessix.tools.search import SearchCapture
from harnessix.tools.search_contracts import GlobInput, GrepInput
from harnessix.tools.windows_read import WindowsReadRuntime
from harnessix.tools.workspace import ReadOperation
from harnessix.workspace.contracts import ResourceAccess


@dataclass(frozen=True)
class _Observed:
    kind: Literal["file", "directory", "missing"]
    identity: tuple[object, ...]
    content: bytes | None
    size: int
    entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...] | None


class _FakeWindowsRoot:
    def __init__(self, path: Path) -> None:
        self.path: Path = path.resolve(strict=True)
        info = self.path.stat()
        self.root_identity: tuple[object, ...] = (info.st_dev, info.st_ino)
        self.closed = False

    def observe(
        self,
        path: str,
        *,
        access: ResourceAccess,
        include_content: bool,
        max_bytes: int,
        checkpoint: Callable[[], None] | None,
    ) -> _Observed:
        del access
        if checkpoint is not None:
            checkpoint()
        target = self.path if path == "." else self.path / Path(*path.split("/"))
        if not target.exists() and not target.is_symlink():
            return _Observed("missing", (path,), None, 0, None)
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise KernelError("workspace_path_denied", "denied")
        if stat.S_ISDIR(info.st_mode):
            entries = []
            identities = []
            for child in target.iterdir():
                if checkpoint is not None:
                    checkpoint()
                child_info = child.lstat()
                if stat.S_ISLNK(child_info.st_mode):
                    kind: Literal["file", "directory", "symlink", "special"] = "symlink"
                elif stat.S_ISDIR(child_info.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(child_info.st_mode):
                    kind = "file"
                else:
                    kind = "special"
                entries.append((child.name, kind))
                identities.append(
                    (child.name.casefold(), kind, child_info.st_dev, child_info.st_ino)
                )
            entries.sort(key=lambda item: item[0].casefold())
            identities.sort()
            return _Observed(
                "directory",
                (info.st_dev, info.st_ino, hashlib.sha256(repr(identities).encode()).hexdigest()),
                repr(identities).encode(),
                len(entries),
                tuple(entries),
            )
        if not stat.S_ISREG(info.st_mode):
            raise KernelError("workspace_wrong_file_type", "wrong")
        content = target.read_bytes() if include_content else None
        if content is not None and len(content) > max_bytes:
            raise KernelError("workspace_snapshot_limit", "limit")
        return _Observed(
            "file",
            (info.st_dev, info.st_ino, info.st_nlink, info.st_size),
            content,
            info.st_size,
            None,
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def runtime(tmp_path: Path) -> WindowsReadRuntime:
    return WindowsReadRuntime(
        tmp_path,
        denied_paths=("private",),
        root_factory=cast(Any, _FakeWindowsRoot),
    )


def test_list_and_read_preserve_public_contract_and_revision(
    tmp_path: Path,
    runtime: WindowsReadRuntime,
) -> None:
    (tmp_path / "b.txt").write_text("第二行\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("第一行\n尾行", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    (tmp_path / "private").mkdir()

    first = runtime.list_files(ListFilesInput(limit=1), ReadOperation())
    assert [item.name for item in first.entries] == ["a.txt"]
    assert first.truncated and first.next_offset == 1
    second = runtime.list_files(
        ListFilesInput(offset=1, expected_revision=first.revision),
        ReadOperation(),
    )
    assert [item.name for item in second.entries] == ["b.txt"]

    page = runtime.read_file(ReadFileInput(path="a.txt", max_lines=1), ReadOperation())
    assert page.text == "第一行\n" and page.next_line == 2
    tail = runtime.read_file(
        ReadFileInput(path="a.txt", start_line=2, expected_revision=page.revision),
        ReadOperation(),
    )
    assert tail.text == "尾行" and not tail.truncated


def test_revision_detects_file_and_directory_change(
    tmp_path: Path,
    runtime: WindowsReadRuntime,
) -> None:
    file = tmp_path / "a.txt"
    file.write_text("old", encoding="utf-8")
    listing = runtime.list_files(ListFilesInput(), ReadOperation())
    page = runtime.read_file(ReadFileInput(path="a.txt"), ReadOperation())

    file.write_text("new", encoding="utf-8")
    with pytest.raises(ReadToolError) as file_changed:
        runtime.read_file(
            ReadFileInput(path="a.txt", start_line=2, expected_revision=page.revision),
            ReadOperation(),
        )
    assert file_changed.value.code == "page_changed"
    (tmp_path / "b.txt").touch()
    with pytest.raises(ReadToolError) as directory_changed:
        runtime.list_files(
            ListFilesInput(offset=1, expected_revision=listing.revision),
            ReadOperation(),
        )
    assert directory_changed.value.code == "page_changed"


def test_glob_grep_and_capture_share_search_budgets(
    tmp_path: Path,
    runtime: WindowsReadRuntime,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("first\nneedle = 1\n", encoding="utf-8")
    (tmp_path / "src/b.txt").write_text("needle\n", encoding="utf-8")
    capture = SearchCapture()

    found = runtime.glob(GlobInput(pattern="**/*.py"), ReadOperation(), capture=capture)
    assert found.paths == ("src/a.py",) and found.scan_complete
    assert b"src/a.py" in capture.body and capture.complete

    result = runtime.grep(
        GrepInput(query="needle", include="**/*.py"),
        ReadOperation(),
        capture=None,
    )
    assert [(item.path, item.line) for item in result.matches] == [("src/a.py", 2)]
    assert result.matches[0].revision
    assert result.stats.files_read == 1 and result.scan_complete


@pytest.mark.parametrize("path", [".env", "private/file", "../outside", "a\\b"])
def test_denied_paths_fail_before_content_is_returned(
    runtime: WindowsReadRuntime,
    path: str,
) -> None:
    with pytest.raises(ReadToolError) as denied:
        runtime.read_file(ReadFileInput(path=path), ReadOperation())
    assert denied.value.code == "path_denied"


def test_links_binary_limits_and_cancellation_fail_closed(
    tmp_path: Path,
    runtime: WindowsReadRuntime,
) -> None:
    outside = tmp_path.parent / "outside-canary"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside)
    (tmp_path / "binary").write_bytes(b"a\0b")
    try:
        with pytest.raises(ReadToolError) as linked:
            runtime.read_file(ReadFileInput(path="link"), ReadOperation())
        assert linked.value.code == "path_denied"
        with pytest.raises(ReadToolError) as binary:
            runtime.read_file(ReadFileInput(path="binary"), ReadOperation())
        assert binary.value.code == "binary_file"
        operation = ReadOperation()
        operation.stopped.set()
        with pytest.raises(TurnCancelled):
            runtime.list_files(ListFilesInput(), operation)
    finally:
        outside.unlink(missing_ok=True)


def test_close_releases_native_root(runtime: WindowsReadRuntime) -> None:
    root = runtime._root
    runtime.close()
    assert root.closed  # type: ignore[attr-defined]
