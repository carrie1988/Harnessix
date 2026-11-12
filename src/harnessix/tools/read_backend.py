"""Coding只读后端装配：按原生平台选择文件与固定Git能力。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from harnessix.agent.errors import KernelError
from harnessix.processes.owner_protocol import OutputRedactionSource
from harnessix.tools import files, git
from harnessix.tools.workspace import ReadOperation, Workspace

if TYPE_CHECKING:
    from harnessix.tools.windows_read import WindowsReadRuntime


@dataclass(frozen=True, slots=True)
class _ReadBackend:
    workspace: Workspace | None
    windows: WindowsReadRuntime | None
    implementation: str
    git_runtime: git.GitReadRuntime | None

    def read_complete_bytes(self, path: str, operation: ReadOperation) -> bytes | None:
        """用本平台的读根读取完整文件字节；超过扫描上限时POSIX返回None，Windows按limit_exceeded失败。"""
        if self.workspace is not None:
            return files.read_complete_bytes(self.workspace, path, operation)
        assert self.windows is not None
        return self.windows.read_complete_bytes(path, operation)


def build_read_backend(
    root: Path,
    denied_paths: tuple[str, ...],
    git_executable: Path | None,
    git_state_directory: Path | None,
    git_output_redaction: OutputRedactionSource | None,
) -> _ReadBackend:
    """先校验Git宿主绑定，再创建唯一文件读根；失败不遗留已开的根句柄。"""
    # Git宿主参数先校验，失败时尚未持有Coding只读根句柄。
    git_runtime = (
        git.GitReadRuntime(
            root,
            git_executable,
            state_directory=git_state_directory,
            output_redaction=git_output_redaction,
        )
        if git_executable is not None
        else None
    )
    if os.name == "nt":
        from harnessix.tools.windows_read import WindowsReadRuntime

        return _ReadBackend(
            workspace=None,
            windows=WindowsReadRuntime(root, denied_paths=denied_paths),
            implementation="coding-read/windows-v1",
            git_runtime=git_runtime,
        )
    if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
        return _ReadBackend(
            workspace=Workspace(root, denied_paths=denied_paths),
            windows=None,
            implementation="coding-read/v1",
            git_runtime=git_runtime,
        )
    raise KernelError(
        "product_tools_platform_unsupported",
        "内置只读Coding Tool Runtime不支持该宿主平台",
    )
