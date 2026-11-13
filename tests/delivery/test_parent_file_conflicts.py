"""三个平台共用的两处判定（Beta Issue #16）：同批“既是文件又是父目录”的整批拒绝，
与缺失父目录之下的写目标观察。

Windows事务端口自ADR 0137第2版起与POSIX一样补建父目录。这两处判定不碰原生句柄，
在任何平台上都能验证；原生句柄的补建与清理见
`tests/delivery/test_windows_create_missing_parents.py`（Windows 11上运行）。
"""

from __future__ import annotations

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.path_case import CASE_CONFLICT, reject_parent_file_conflicts
from harnessix.workspace import snapshot
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import _Observed, observe_creatable_resource


@pytest.mark.parametrize(
    "paths",
    [("pkg", "pkg/a.py"), ("Pkg", "pkg/sub/a.py"), ("pkg/a.py", "PKG")],
    ids=["same-case", "different-case", "file-listed-second"],
)
def test_windows_rejects_a_path_that_is_a_file_and_a_parent_in_one_batch(paths) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(KernelError) as refused:
        reject_parent_file_conflicts(paths, "windows")
    assert refused.value.code == CASE_CONFLICT
    assert "一个路径不能既是文件又是另一项的父目录" in str(refused.value.message)


def test_windows_accepts_siblings_and_shared_new_parents() -> None:
    reject_parent_file_conflicts(("pkg/a.py", "pkg/b.py", "pkg2", "pkg.txt", "PKG/c.py"), "windows")


class _WindowsLikeRoot:
    """与Windows端口相同的观察语义：父目录存在时缺失资源绑定父目录身份，父目录不存在时报parent_missing。"""

    def __init__(self, directories: set[str], files: set[str]) -> None:
        self.directories, self.files = directories, files

    def observe(self, path: str, *, access: str, checkpoint: object = None) -> _Observed:
        if path in self.directories:
            return _Observed("directory", ("directory", path), b"", 0)
        if path in self.files:
            return _Observed("file", ("file", path), b"x", 1)
        parent, _, name = path.rpartition("/")
        if parent in self.directories:
            return _Observed("missing", ("directory", parent, name), None, 0)
        raise KernelError("workspace_parent_missing", "Windows缺失资源的父目录不存在")


def test_a_write_target_below_missing_directories_binds_the_nearest_existing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 在本机以假根代替WindowsWorkspaceRoot：路径按Windows规则切分，身份绑定最近的已有目录。
    monkeypatch.setattr(snapshot, "creates_missing_parents", lambda _native: True)
    root = _WindowsLikeRoot({"", "src"}, {"src/a.py"})
    request = WorkspaceResourceRequest(path="Pkg/Sub/new.py", access="write")
    observed = observe_creatable_resource(
        root,  # type: ignore[arg-type]
        request,
        KernelError("workspace_parent_missing", "Windows缺失资源的父目录不存在"),
        (request,),
        "windows",
    )
    assert observed.kind == "missing"
    assert observed.identity == ("directory", "", "Pkg", "Sub", "new.py")


def test_a_port_that_does_not_create_parents_keeps_the_rejection() -> None:
    """既不是POSIX根也不是Windows根的端口（例如这个假根）保留原拒绝。"""
    request = WorkspaceResourceRequest(path="Pkg/new.py", access="write")
    rejection = KernelError("workspace_parent_missing", "Windows缺失资源的父目录不存在")
    with pytest.raises(KernelError) as kept:
        observe_creatable_resource(
            _WindowsLikeRoot({""}, set()),  # type: ignore[arg-type]
            request,
            rejection,
            (request,),
            "windows",
        )
    assert kept.value is rejection
