"""Context读取门面：复用POSIX FD或Windows Handle只读能力，不增加文件权限。"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Self

from harnessix.agent.errors import KernelError
from harnessix.tools import files
from harnessix.tools.contracts import (
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    ReadToolError,
)
from harnessix.tools.windows_read import WindowsReadRuntime
from harnessix.tools.workspace import (
    ReadOperation,
    Workspace,
    WorkspaceReadPolicy,
    digest,
    revision_state,
)


def _open_reader(root: Path, denied_paths: tuple[str, ...]) -> Workspace | WindowsReadRuntime:
    """只选择本机安全端口；Windows失败不得退回普通Path读取。"""
    if os.name == "nt":
        try:
            return WindowsReadRuntime(root, denied_paths=denied_paths)
        except KernelError:
            raise ReadToolError("path_denied") from None
    return Workspace(root, denied_paths=denied_paths)


class ContextReadWorkspace:
    """为动态Context提供共享逻辑路径、分页、revision及受控资源生命周期。"""

    def __init__(self, root: Path, *, denied_paths: tuple[str, ...] = ()) -> None:
        self._policy = WorkspaceReadPolicy(denied_paths)
        self._reader = _open_reader(root, denied_paths)
        self.scope = self._reader.scope

    def parts(self, path: str) -> tuple[str, ...]:
        """保持与Coding只读工具一致的敏感名称和越界路径拒绝规则。"""
        return self._policy.parts(path)

    def directory_revision(self, path: str, operation: ReadOperation) -> str:
        """POSIX保持历史revision；Windows复用完整稳定目录观察而非路径stat。"""
        if isinstance(self._reader, Workspace):
            with self._reader.open(path, operation, directory=True) as descriptor:
                return digest((self.scope, path, revision_state(os.fstat(descriptor))))
        return self._reader.list_files(ListFilesInput(path=path, limit=1), operation).revision

    def list_files(self, args: ListFilesInput, operation: ReadOperation) -> ListFilesOutput:
        """有界目录页读取；后续页仍须携带既有revision。"""
        if isinstance(self._reader, Workspace):
            return files.list_files(self._reader, args, operation)
        return self._reader.list_files(args, operation)

    def read_file(self, args: ReadFileInput, operation: ReadOperation) -> ReadFileOutput:
        """保持分页合同，仅把Windows明确缺失统一为项目指令候选缺失。"""
        if isinstance(self._reader, Workspace):
            return files.read_file(self._reader, args, operation)
        try:
            return self._reader.read_file(args, operation)
        except ReadToolError as error:
            if error.code == "not_found":
                raise FileNotFoundError from None
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._reader.close()
