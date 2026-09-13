"""Windows原生只读工具门面：保持既有Tool Runtime调用合同。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from harnessix.tools import search
from harnessix.tools.contracts import (
    ListFilesInput,
    ListFilesOutput,
    ReadContract,
    ReadFileInput,
    ReadFileOutput,
)
from harnessix.tools.search_contracts import GlobInput, GlobOutput, GrepInput, GrepOutput
from harnessix.tools.windows_file_read import list_files, read_file
from harnessix.tools.windows_read_port import WindowsReadPort, WindowsRoot
from harnessix.tools.windows_search import glob_files, grep_files
from harnessix.tools.workspace import ReadOperation
from harnessix.workspace.windows import WindowsWorkspaceRoot


class WindowsReadRuntime:
    """持有Windows根Handle，把四种只读调用委派给独立实现。"""

    def __init__(
        self,
        root: Path,
        *,
        denied_paths: tuple[str, ...] = (),
        root_factory: Callable[[Path], WindowsRoot] = WindowsWorkspaceRoot,
    ) -> None:
        self._port = WindowsReadPort(
            root,
            denied_paths=denied_paths,
            root_factory=root_factory,
        )
        self._root = self._port.root_handle
        self.root = self._port.root
        self.scope = self._port.scope

    def inspect(self, operation: ReadOperation) -> str:
        return self._port.inspect(operation)

    def execute(
        self,
        args: ListFilesInput | ReadFileInput | GlobInput | GrepInput,
        operation: ReadOperation,
        *,
        capture: search.SearchCapture | None = None,
    ) -> ReadContract:
        if isinstance(args, ListFilesInput):
            return self.list_files(args, operation)
        if isinstance(args, GlobInput):
            return self.glob(args, operation, capture=capture)
        if isinstance(args, GrepInput):
            return self.grep(args, operation, capture=capture)
        return self.read_file(args, operation)

    def list_files(self, args: ListFilesInput, operation: ReadOperation) -> ListFilesOutput:
        return list_files(self._port, args, operation)

    def read_file(self, args: ReadFileInput, operation: ReadOperation) -> ReadFileOutput:
        return read_file(self._port, args, operation)

    def glob(
        self,
        args: GlobInput,
        operation: ReadOperation,
        *,
        capture: search.SearchCapture | None,
    ) -> GlobOutput:
        return glob_files(self._port, args, operation, capture=capture)

    def grep(
        self,
        args: GrepInput,
        operation: ReadOperation,
        *,
        capture: search.SearchCapture | None,
    ) -> GrepOutput:
        return grep_files(self._port, args, operation, capture=capture)

    def close(self) -> None:
        self._port.close()
