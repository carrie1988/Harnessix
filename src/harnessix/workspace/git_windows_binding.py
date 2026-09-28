"""Windows Git只读绑定：固定原生根、可执行文件及其句柄身份。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.tools.workspace import digest as canonical_digest
from harnessix.workspace.windows import WindowsWorkspaceRoot


def validate_windows_git_executable(path: Path) -> Path:
    """Doctor与执行端共用绝对EXE校验；不解析掉Junction后再校验。"""
    if os.name != "nt":
        raise KernelError("product_git_platform_unsupported", "Windows Git需要原生宿主")
    if (
        not path.is_absolute()
        or path.suffix.casefold() != ".exe"
        or any(ord(value) < 32 or ord(value) == 127 for value in str(path))
    ):
        raise KernelError("product_git_invalid", "Git必须绑定绝对普通EXE文件")
    try:
        with _executable_handle(path):
            return path
    except (OSError, KernelError):
        raise KernelError("product_git_invalid", "Git原生可执行文件绑定无效") from None


@contextmanager
def _executable_handle(path: Path) -> Iterator[tuple[WindowsWorkspaceRoot, int]]:
    with ExitStack() as resources:
        parent = WindowsWorkspaceRoot(path.parent)
        resources.callback(parent.close)
        # 与Workspace端口共用父链和叶句柄，不引入另一套CreateFile边界。
        handles, _ = parent._open_chain(path.name)  # noqa: SLF001
        resources.callback(parent._close_all, handles)  # noqa: SLF001
        info = parent._information(handles[-1])  # noqa: SLF001
        if info.attributes & 0x410 or info.links != 1:
            raise KernelError("product_git_invalid", "Git不能是目录、链接或多硬链接文件")
        yield parent, handles[-1]


@dataclass(frozen=True)
class WindowsGitBinding:
    """一个固定命令期间同时持有Workspace及EXE；并非隔离恶意同UID宿主。"""

    root: WindowsWorkspaceRoot
    executable_parent: WindowsWorkspaceRoot
    executable_handle: int
    fingerprint: str

    def verify(self) -> None:
        self.root.observe(".", access="read")
        current = _fingerprint(self.root, self.executable_parent, self.executable_handle)
        if current != self.fingerprint:
            raise KernelError("process_binding_changed", "Git只读绑定在执行期间变化")


def _fingerprint(root: WindowsWorkspaceRoot, parent: WindowsWorkspaceRoot, handle: int) -> str:
    info = parent._information(handle)  # noqa: SLF001 - 同一原生句柄的前后身份
    return canonical_digest(
        {
            "workspace_path": os.path.normcase(str(root.path)),
            "workspace_identity": root.root_identity,
            "executable_parent": os.path.normcase(str(parent.path)),
            "executable_identity": parent._revision_identity(info),  # noqa: SLF001
        }
    )


@contextmanager
def pin_windows_git(root: Path, executable: Path) -> Iterator[WindowsGitBinding]:
    """完整持有句柄直至命令终结；关闭时只释放自己打开的句柄。"""
    validate_windows_git_executable(executable)
    with ExitStack() as resources:
        workspace = WindowsWorkspaceRoot(root)
        resources.callback(workspace.close)
        parent, handle = resources.enter_context(_executable_handle(executable))
        yield WindowsGitBinding(workspace, parent, handle, _fingerprint(workspace, parent, handle))


@contextmanager
def pin_windows_git_state(path: Path) -> Iterator[None]:
    """状态目录创建前固定现有原生父链；已有Junction不能先被resolve隐藏。"""
    ancestor = path
    while not ancestor.exists():
        if ancestor.is_symlink() or ancestor.is_junction() or ancestor.parent == ancestor:
            raise KernelError("product_state_invalid", "Git私有状态父链无效")
        ancestor = ancestor.parent
    root = WindowsWorkspaceRoot(ancestor)
    try:
        yield
    finally:
        root.close()
