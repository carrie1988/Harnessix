"""一次读取操作内复用已核对的目录句柄（Beta Issue #21，Windows）。

ADR 0160之后，Windows真机上16000个文件的整仓grep仍约需9秒：每个文件都从根逐段
打开父目录。现在每个目录在一次操作里只打开并核对一次。这里用替身文件系统验证缓存的
逻辑；真实句柄语义（Junction、硬链接、长路径）由test_windows_native_runtime.py
在Windows上验证。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.windows_read_port import WindowsReadPort, directory_scope
from harnessix.tools.workspace import ReadOperation
from harnessix.workspace import windows_directory_cache
from harnessix.workspace.windows import WindowsWorkspaceRoot
from harnessix.workspace.windows_directory_cache import DirectoryHandles

_ATTRIBUTES = {"dir": 0x10, "file": 0x0, "reparse": 0x410, "outside": 0x10}


class _FakeRoot(WindowsWorkspaceRoot):
    """按工作区相对路径记录每次打开的Windows根替身；不调用任何系统接口。"""

    def __init__(self, entries: dict[str, str]) -> None:
        self.path = Path("C:/repo")
        self.root_identity = (1, 2, 3)
        self._final_root = "\\\\?\\c:\\repo"
        self.entries = entries
        self.opened: list[str] = []
        self.shared_writes: dict[str, bool] = {}
        self.open_handles: dict[int, str] = {}
        self.root_checks = 0
        self.root_changed = False
        self._next_handle = 100

    def _open(self, path: Path, *, data: bool, directory: bool = False) -> int:
        name = path.relative_to(self.path).as_posix()
        if name not in self.entries:
            raise FileNotFoundError(2, "missing", str(path))
        self._next_handle += 1
        self.opened.append(name)
        self.shared_writes[name] = directory
        self.open_handles[self._next_handle] = name
        return self._next_handle

    def _information(self, handle: int) -> Any:
        return SimpleNamespace(attributes=_ATTRIBUTES[self.entries[self.open_handles[handle]]])

    def _final_path(self, handle: int) -> str:
        name = self.open_handles[handle]
        base = "\\\\?\\c:\\elsewhere" if self.entries[name] == "outside" else self._final_root
        return base + "\\" + name.replace("/", "\\")

    def _close_all(self, handles: list[int]) -> None:
        for handle in handles:
            self.open_handles.pop(handle, None)

    def _open_chain(
        self,
        logical_path: str,
        *,
        data: bool = True,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[list[int], str]:
        assert logical_path == "."
        self.root_checks += 1
        if self.root_changed:
            raise KernelError("workspace_changed", "Windows Workspace根已经变化")
        return [], self._final_root

    def observe(self, path: str, **_: object) -> Any:
        return ("full-chain", path)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _record_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    def observed(root: _FakeRoot, handles: list[int], final_path: str, normalized: str, **_: Any):
        root._assert_under_root(final_path)
        return ("cached", normalized, [root.open_handles[handle] for handle in handles])

    monkeypatch.setattr(windows_directory_cache, "_observe_opened", observed)


def _observe(cache: DirectoryHandles, path: str) -> Any:
    return cache.observe(path, include_content=True, max_bytes=1024, checkpoint=None)


def _repository() -> _FakeRoot:
    return _FakeRoot(
        {
            "web": "dir",
            "web/hooks": "dir",
            "web/hooks/a.ts": "file",
            "web/hooks/b.ts": "file",
            "web/app": "dir",
            "web/app/c.ts": "file",
        }
    )


def test_each_directory_is_opened_and_checked_once_per_operation() -> None:
    root = _repository()
    cache = DirectoryHandles(root)
    for path in ("web/hooks/a.ts", "web/hooks/b.ts", "web/app/c.ts"):
        assert _observe(cache, path) == ("cached", path, [path])
    # 此前每个文件都从根逐段打开：web、web/hooks、文件，共9次；现在每个目录只打开一次。
    assert root.opened == [
        "web",
        "web/hooks",
        "web/hooks/a.ts",
        "web/hooks/b.ts",
        "web/app",
        "web/app/c.ts",
    ]
    assert root.root_checks == 1
    # 目录句柄与会话根一样共享读写（编辑器仍可在其中新建、改名文件），文件只共享读。
    assert root.shared_writes == {
        "web": True,
        "web/hooks": True,
        "web/hooks/a.ts": False,
        "web/hooks/b.ts": False,
        "web/app": True,
        "web/app/c.ts": False,
    }
    assert sorted(root.open_handles.values()) == ["web", "web/app", "web/hooks"]
    cache.verify_root()
    assert root.root_checks == 2
    cache.close()
    assert root.open_handles == {}


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ({"web": "reparse", "web/a.ts": "file"}, "workspace_path_denied"),
        ({"web": "outside", "web/a.ts": "file"}, "workspace_path_denied"),
        ({"web": "file", "web/a.ts": "file"}, "workspace_wrong_file_type"),
        ({"web": "dir", "web/a.ts": "reparse"}, "workspace_path_denied"),
    ],
)
def test_a_junction_a_foreign_target_or_a_file_in_the_chain_is_refused(
    entries: dict[str, str], code: str
) -> None:
    root = _FakeRoot(entries)
    cache = DirectoryHandles(root)
    with pytest.raises(KernelError) as raised:
        _observe(cache, "web/a.ts")
    assert raised.value.code == code
    cache.close()
    assert root.open_handles == {}


def test_missing_paths_the_root_itself_and_the_cap_take_the_full_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository()
    cache = DirectoryHandles(root)
    assert _observe(cache, ".") is None
    assert _observe(cache, "web/hooks/missing.ts") is None
    assert _observe(cache, "missing/a.ts") is None
    monkeypatch.setattr(windows_directory_cache, "MAX_CACHED_DIRECTORIES", 2)
    assert _observe(cache, "web/hooks/a.ts") == ("cached", "web/hooks/a.ts", ["web/hooks/a.ts"])
    assert _observe(cache, "web/app/c.ts") is None  # 已达上限：web/app不再缓存
    cache.close()
    assert root.open_handles == {}


def test_the_root_is_checked_at_the_start_and_at_the_end() -> None:
    unused = _repository()
    DirectoryHandles(unused).verify_root()
    assert unused.root_checks == 0  # 没用过缓存：不额外核对

    replaced_before = _repository()
    replaced_before.root_changed = True
    with pytest.raises(KernelError, match="根已经变化"):
        _observe(DirectoryHandles(replaced_before), "web/hooks/a.ts")
    assert replaced_before.opened == []

    replaced_during = _repository()
    cache = DirectoryHandles(replaced_during)
    _observe(cache, "web/hooks/a.ts")
    replaced_during.root_changed = True
    with pytest.raises(KernelError, match="根已经变化"):
        cache.verify_root()


def test_the_cache_belongs_to_one_operation_and_a_changed_root_voids_the_result(
    tmp_path: Path,
) -> None:
    root = _repository()
    port = WindowsReadPort(tmp_path, denied_paths=(), root_factory=lambda _: root)
    operation, other = ReadOperation(), ReadOperation()
    with directory_scope(port, operation):
        assert operation.windows_directories is not None and other.windows_directories is None
        with directory_scope(port, operation):  # 嵌套：不重复建立
            pass
        assert operation.windows_directories is not None
        assert port.observe("web/hooks/a.ts", operation) == (
            "cached",
            "web/hooks/a.ts",
            ["web/hooks/a.ts"],
        )
        assert port.observe("web/hooks/a.ts", other) == ("full-chain", "web/hooks/a.ts")
    assert operation.windows_directories is None and root.open_handles == {}

    with pytest.raises(ReadToolError, match="workspace_changed"):
        with directory_scope(port, operation):
            port.observe("web/app/c.ts", operation)
            root.root_changed = True
    assert operation.windows_directories is None and root.open_handles == {}

    root.root_changed = False
    with pytest.raises(RuntimeError):
        with directory_scope(port, operation):
            port.observe("web/app/c.ts", operation)
            raise RuntimeError
    assert root.open_handles == {}


def test_doubles_that_are_not_real_windows_roots_keep_the_full_chain(tmp_path: Path) -> None:
    double = SimpleNamespace(path=tmp_path, root_identity=(1,), close=lambda: None)
    port = WindowsReadPort(tmp_path, denied_paths=(), root_factory=cast(Any, lambda _: double))
    operation = ReadOperation()
    with directory_scope(port, operation):
        assert operation.windows_directories is None
