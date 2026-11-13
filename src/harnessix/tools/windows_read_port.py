"""Windows只读工具的安全观察端口与稳定错误映射。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Protocol, cast

from harnessix.agent.errors import KernelError
from harnessix.tools.contracts import MAX_SCAN_BYTES, ReadErrorCode, ReadToolError
from harnessix.tools.workspace import ReadOperation, WorkspaceReadPolicy, digest
from harnessix.workspace.windows import WindowsWorkspaceRoot, windows_port_source_digest
from harnessix.workspace.windows_directory_cache import DirectoryHandles


class Observation(Protocol):
    """Windows资源一次稳定观察所需的最小内部结构。"""

    kind: Literal["file", "directory", "missing"]
    identity: tuple[object, ...]
    content: bytes | None
    size: int
    entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...] | None


class WindowsRoot(Protocol):
    """可由真实Handle端口或测试替身实现的Windows根能力。"""

    path: Path
    root_identity: tuple[object, ...]

    def observe(
        self,
        path: str,
        *,
        access: Literal["read", "write", "execute"],
        include_content: bool,
        max_bytes: int,
        checkpoint: Callable[[], None] | None,
    ) -> Observation: ...

    def close(self) -> None: ...


def require_kind(observed: Observation, expected: Literal["file", "directory"]) -> None:
    """把缺失和类型不符映射为公共只读工具错误。"""

    if observed.kind == "missing":
        raise ReadToolError("not_found")
    if observed.kind != expected:
        raise ReadToolError("wrong_file_type")


def same_observation(first: Observation, second: Observation) -> bool:
    """判断两次观察的全部合同字段是否相同。"""

    return (
        first.kind == second.kind
        and first.identity == second.identity
        and first.content == second.content
        and first.size == second.size
        and first.entries == second.entries
    )


def relative_path(path: str, root: str) -> str:
    """返回相对于搜索起点的逻辑路径。"""

    if root == ".":
        return path
    if path == root:
        return path.rsplit("/", 1)[-1]
    return path[len(root) + 1 :]


def file_revision(port: WindowsReadPort, path: str, observed: Observation) -> str:
    """把范围、对象身份、大小与内容摘要绑定为文件revision。"""

    if observed.content is None:
        raise ReadToolError("io_failed")
    return digest(
        (
            port.scope,
            path,
            observed.identity,
            observed.size,
            hashlib.sha256(observed.content).hexdigest(),
        )
    )


def _read_error(error: KernelError) -> ReadToolError:
    code = {
        "workspace_path_denied": "path_denied",
        "workspace_wrong_file_type": "wrong_file_type",
        "workspace_parent_missing": "not_found",
        "workspace_snapshot_limit": "limit_exceeded",
        "workspace_changed": "workspace_changed",
    }.get(error.code, "io_failed")
    return ReadToolError(cast(ReadErrorCode, code))


@contextmanager
def directory_scope(port: WindowsReadPort, operation: ReadOperation) -> Iterator[None]:
    """一次读取操作内复用已核对的目录句柄（Beta Issue #21）；只用于真实的Windows根。"""

    root = port.root_handle
    if not isinstance(root, WindowsWorkspaceRoot) or operation.windows_directories is not None:
        yield
        return
    directories = DirectoryHandles(root)
    operation.windows_directories = directories
    try:
        yield
    finally:
        operation.windows_directories = None
        directories.close()
    try:
        directories.verify_root()
    except KernelError as error:
        raise _read_error(error) from None


def session_workspace_root(root: Path) -> WindowsWorkspaceRoot:
    """会话期间一直持有的工作区根：用户的编辑器与构建工具仍可在根目录内新建、改名文件。"""

    return WindowsWorkspaceRoot(root, allow_renames_inside=True)


class WindowsReadPort:
    """封装路径策略、Handle观察和跨观察一致性判断。"""

    def __init__(
        self,
        root: Path,
        *,
        denied_paths: tuple[str, ...],
        root_factory: Callable[[Path], WindowsRoot] = session_workspace_root,
    ) -> None:
        self.policy = WorkspaceReadPolicy(denied_paths)
        self.root_handle = root_factory(root)
        self.root = self.root_handle.path
        self.scope = digest(
            {
                "policy": "windows-workspace-read/v1",
                "root": str(self.root).casefold(),
                "identity": self.root_handle.root_identity,
                **self.policy.scope_fields(),
                "windows_port_source_digest": windows_port_source_digest(),
            }
        )

    def path(self, path: str) -> str:
        parts = self.policy.parts(path)
        return "/".join(parts) if parts else "."

    def observe(
        self,
        path: str,
        operation: ReadOperation,
        *,
        include_content: bool = True,
        max_bytes: int = MAX_SCAN_BYTES,
    ) -> Observation:
        operation.checkpoint()
        directories = operation.windows_directories
        try:
            if directories is not None:
                observed = directories.observe(
                    path,
                    include_content=include_content,
                    max_bytes=max_bytes,
                    checkpoint=operation.checkpoint,
                )
                if observed is not None:
                    return observed
            return self.root_handle.observe(
                path,
                access="read",
                include_content=include_content,
                max_bytes=max_bytes,
                checkpoint=operation.checkpoint,
            )
        except KernelError as error:
            raise _read_error(error) from None

    def observe_candidate(
        self,
        path: str,
        operation: ReadOperation,
        *,
        content: bool,
        max_bytes: int,
    ) -> Observation:
        try:
            observed = self.observe(
                path,
                operation,
                include_content=content,
                max_bytes=max_bytes,
            )
            require_kind(observed, "file")
            return observed
        except ReadToolError as error:
            if error.code in {"not_found", "path_denied", "wrong_file_type"}:
                raise ReadToolError("workspace_changed") from None
            raise

    def inspect(self, operation: ReadOperation) -> str:
        observed = self.observe(".", operation, include_content=False)
        require_kind(observed, "directory")
        return self.scope

    def close(self) -> None:
        self.root_handle.close()
